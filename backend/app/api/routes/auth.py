import hmac
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.auth_schemas import (
    BootstrapStatusResponse,
    EmailChangeRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    PasswordChangeRequest,
    RegisterRequest,
    SensitiveVerifyRequest,
    SessionRead,
    TotpCodeRequest,
    TotpLoginRequest,
    TotpSetupResponse,
    UserRead,
)
from app.core.config import get_settings
from app.db import get_session
from app.models import AuthSession, LoginChallenge, User
from app.services.crypto import CredentialCipher, EncryptionNotConfigured
from app.services.security import (
    add_security_event,
    as_utc,
    clear_auth_cookies,
    clear_login_failures,
    client_ip,
    create_session,
    generate_totp_secret,
    hash_password,
    new_token,
    normalize_email,
    password_needs_rehash,
    record_login_failure,
    retry_after_seconds,
    set_auth_cookies,
    token_hash,
    totp_uri,
    user_agent,
    utc_now,
    verify_password,
    verify_totp,
)

router = APIRouter()
GENERIC_LOGIN_ERROR = "邮箱、密码或验证码不正确"


def user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        two_factor_enabled=user.two_factor_enabled,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


def current_user(request: Request) -> User:
    return request.state.user


def current_session(request: Request) -> AuthSession:
    return request.state.auth_session


def encryption_cipher() -> CredentialCipher:
    try:
        return CredentialCipher(get_settings().master_encryption_key)
    except EncryptionNotConfigured as error:
        raise HTTPException(status_code=503, detail="安全密钥尚未配置") from error


@router.get("/bootstrap-status", response_model=BootstrapStatusResponse)
def bootstrap_status(session: Session = Depends(get_session)) -> BootstrapStatusResponse:
    user_count = session.scalar(select(func.count()).select_from(User)) or 0
    return BootstrapStatusResponse(
        registration_available=user_count == 0 and bool(get_settings().auth_bootstrap_token)
    )


@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, response: Response, session: Session = Depends(get_session)) -> LoginResponse:
    settings = get_settings()
    user_count = session.scalar(select(func.count()).select_from(User)) or 0
    if user_count:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员账户已创建，公开注册已关闭")
    bootstrap_token = request.headers.get("x-bootstrap-token", "")
    if not settings.auth_bootstrap_token or not hmac.compare_digest(bootstrap_token, settings.auth_bootstrap_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="首次初始化需要有效的启动令牌")
    email = normalize_email(str(payload.email))
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        registration_slot="primary",
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册或管理员账户已创建") from error
    user.last_login_at = utc_now()
    auth_session, raw_token, raw_csrf = create_session(session, request, user, remember_me=False)
    add_security_event(session, request, "account_registered", user.id)
    session.commit()
    set_auth_cookies(response, raw_token, raw_csrf, auth_session.expires_at)
    return LoginResponse(authenticated=True, user=user_read(user))


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)) -> LoginResponse:
    email = normalize_email(str(payload.email))
    ip = client_ip(request)
    retry = retry_after_seconds(session, email, ip)
    if retry:
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后重试", headers={"Retry-After": str(retry)})
    user = session.scalar(select(User).where(User.email == email))
    password_ok = verify_password(user.password_hash if user else None, payload.password)
    if not user or not password_ok:
        retry = record_login_failure(session, email, ip)
        add_security_event(session, request, "login_failed", user.id if user else None)
        session.commit()
        if retry:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后重试", headers={"Retry-After": str(retry)})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=GENERIC_LOGIN_ERROR)

    clear_login_failures(session, email, ip)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    if user.two_factor_enabled:
        raw_challenge = new_token()
        now = utc_now()
        session.add(
            LoginChallenge(
                user_id=user.id,
                token_hash=token_hash(raw_challenge),
                remember_me=payload.remember_me,
                ip_address=ip,
                user_agent=user_agent(request),
                created_at=now,
                expires_at=now + timedelta(minutes=get_settings().auth_login_challenge_minutes),
            )
        )
        add_security_event(session, request, "password_verified_totp_pending", user.id)
        session.commit()
        return LoginResponse(authenticated=False, totp_required=True, challenge=raw_challenge)

    user.last_login_at = utc_now()
    auth_session, raw_token, raw_csrf = create_session(session, request, user, payload.remember_me)
    add_security_event(session, request, "login_succeeded", user.id)
    session.commit()
    set_auth_cookies(response, raw_token, raw_csrf, auth_session.expires_at)
    return LoginResponse(authenticated=True, user=user_read(user))


@router.post("/login/totp", response_model=LoginResponse)
def login_totp(payload: TotpLoginRequest, request: Request, response: Response, session: Session = Depends(get_session)) -> LoginResponse:
    challenge = session.scalar(select(LoginChallenge).where(LoginChallenge.token_hash == token_hash(payload.challenge)))
    now = utc_now()
    if not challenge or challenge.consumed_at or as_utc(challenge.expires_at) <= now or challenge.attempts >= 5:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=GENERIC_LOGIN_ERROR)
    if challenge.ip_address and not hmac.compare_digest(challenge.ip_address, client_ip(request)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=GENERIC_LOGIN_ERROR)
    user = session.get(User, challenge.user_id)
    if not user or not user.two_factor_enabled or not user.totp_secret_encrypted:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=GENERIC_LOGIN_ERROR)
    try:
        secret = encryption_cipher().decrypt(user.totp_secret_encrypted)
    except Exception as error:
        raise HTTPException(status_code=503, detail="双因素验证暂时不可用") from error
    if not verify_totp(secret, payload.code):
        challenge.attempts += 1
        add_security_event(session, request, "totp_login_failed", user.id)
        session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=GENERIC_LOGIN_ERROR)

    challenge.consumed_at = now
    user.last_login_at = now
    auth_session, raw_token, raw_csrf = create_session(session, request, user, challenge.remember_me)
    add_security_event(session, request, "login_succeeded_with_totp", user.id)
    session.commit()
    set_auth_cookies(response, raw_token, raw_csrf, auth_session.expires_at)
    return LoginResponse(authenticated=True, user=user_read(user))


@router.get("/me", response_model=UserRead)
def me(request: Request) -> UserRead:
    return user_read(current_user(request))


@router.post("/logout", response_model=MessageResponse)
def logout(request: Request, response: Response, session: Session = Depends(get_session)) -> MessageResponse:
    auth_session = current_session(request)
    auth_session.revoked_at = utc_now()
    add_security_event(session, request, "session_logged_out", auth_session.user_id, {"session_id": str(auth_session.id)})
    session.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="已安全退出")


@router.get("/sessions", response_model=list[SessionRead])
def list_sessions(request: Request, session: Session = Depends(get_session)) -> list[SessionRead]:
    active = session.scalars(
        select(AuthSession)
        .where(AuthSession.user_id == current_user(request).id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > utc_now())
        .order_by(AuthSession.last_seen_at.desc())
    )
    current_id = current_session(request).id
    return [
        SessionRead(
            id=item.id,
            created_at=item.created_at,
            last_seen_at=item.last_seen_at,
            expires_at=item.expires_at,
            ip_address=item.ip_address,
            user_agent=item.user_agent,
            current=item.id == current_id,
        )
        for item in active
    ]


@router.delete("/sessions/{session_id}", response_model=MessageResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def revoke_session(session_id: UUID, request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    target = session.scalar(
        select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == current_user(request).id)
    )
    if not target:
        raise HTTPException(status_code=404, detail="会话不存在")
    target.revoked_at = utc_now()
    add_security_event(session, request, "session_revoked", current_user(request).id, {"session_id": str(target.id)})
    session.commit()
    return MessageResponse(message="设备会话已退出")


@router.post("/sessions/logout-others", response_model=MessageResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def logout_others(request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    current = current_session(request)
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == current.user_id, AuthSession.id != current.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    add_security_event(session, request, "other_sessions_revoked", current.user_id)
    session.commit()
    return MessageResponse(message="其他设备已全部退出")


@router.post("/sensitive/verify", response_model=MessageResponse)
def verify_sensitive(payload: SensitiveVerifyRequest, request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    user = current_user(request)
    valid = verify_password(user.password_hash, payload.current_password)
    if valid and user.two_factor_enabled:
        if not payload.totp_code or not user.totp_secret_encrypted:
            valid = False
        else:
            try:
                valid = verify_totp(encryption_cipher().decrypt(user.totp_secret_encrypted), payload.totp_code)
            except Exception:
                valid = False
    if not valid:
        add_security_event(session, request, "sensitive_auth_failed", user.id)
        session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="当前密码或验证码不正确")
    current_session(request).last_sensitive_auth_at = utc_now()
    add_security_event(session, request, "sensitive_auth_succeeded", user.id)
    session.commit()
    return MessageResponse(message="安全验证通过，10 分钟内有效")


@router.post("/totp/setup", response_model=TotpSetupResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def setup_totp(request: Request, session: Session = Depends(get_session)) -> TotpSetupResponse:
    user = current_user(request)
    if user.two_factor_enabled:
        raise HTTPException(status_code=409, detail="双因素验证已启用")
    secret = generate_totp_secret()
    user.pending_totp_secret_encrypted = encryption_cipher().encrypt(secret)
    add_security_event(session, request, "totp_setup_started", user.id)
    session.commit()
    return TotpSetupResponse(secret=secret, provisioning_uri=totp_uri(secret, user.email))


@router.post("/totp/confirm", response_model=MessageResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def confirm_totp(payload: TotpCodeRequest, request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    user = current_user(request)
    if not user.pending_totp_secret_encrypted:
        raise HTTPException(status_code=409, detail="请先开始设置双因素验证")
    try:
        secret = encryption_cipher().decrypt(user.pending_totp_secret_encrypted)
    except Exception as error:
        raise HTTPException(status_code=503, detail="双因素验证暂时不可用") from error
    if not verify_totp(secret, payload.code):
        raise HTTPException(status_code=422, detail="验证码不正确")
    user.totp_secret_encrypted = user.pending_totp_secret_encrypted
    user.pending_totp_secret_encrypted = None
    user.two_factor_enabled = True
    add_security_event(session, request, "totp_enabled", user.id)
    session.commit()
    return MessageResponse(message="双因素验证已启用")


@router.delete("/totp", response_model=MessageResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def disable_totp(request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    user = current_user(request)
    user.two_factor_enabled = False
    user.totp_secret_encrypted = None
    user.pending_totp_secret_encrypted = None
    user.totp_last_counter = None
    add_security_event(session, request, "totp_disabled", user.id)
    session.commit()
    return MessageResponse(message="双因素验证已关闭")


@router.post("/password", response_model=MessageResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def change_password(payload: PasswordChangeRequest, request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    user = current_user(request)
    user.password_hash = hash_password(payload.new_password)
    current = current_session(request)
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.id != current.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    add_security_event(session, request, "password_changed", user.id)
    session.commit()
    return MessageResponse(message="密码已更新，其他设备已退出")


@router.post("/email", response_model=MessageResponse, dependencies=[Depends(require_recent_sensitive_auth)])
def change_email(payload: EmailChangeRequest, request: Request, session: Session = Depends(get_session)) -> MessageResponse:
    user = current_user(request)
    user.email = normalize_email(str(payload.new_email))
    try:
        add_security_event(session, request, "email_changed", user.id)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="该邮箱已被使用") from error
    return MessageResponse(message="登录邮箱已更新")
