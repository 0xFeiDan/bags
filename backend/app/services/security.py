import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AuthSession, LoginAttempt, SecurityEvent, User

password_hasher = PasswordHasher()
_dummy_password_hash = password_hasher.hash("bags-dummy-password-not-a-user")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    target = password_hash or _dummy_password_hash
    try:
        return password_hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return password_hasher.check_needs_rehash(password_hash)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def client_ip(request: Request) -> str:
    settings = get_settings()
    if settings.auth_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:64]
    return (request.client.host if request.client else "unknown")[:64]


def user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:512] if value else None


def identifier_hash(scope: str, value: str) -> str:
    settings = get_settings()
    key_material = settings.master_encryption_key or f"development:{settings.app_name}"
    return hmac.new(key_material.encode("utf-8"), f"{scope}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _attempt(session: Session, scope: str, value: str) -> LoginAttempt | None:
    digest = identifier_hash(scope, value)
    return session.scalar(select(LoginAttempt).where(LoginAttempt.scope == scope, LoginAttempt.identifier_hash == digest))


def retry_after_seconds(session: Session, email: str, ip: str) -> int:
    now = utc_now()
    retry = 0
    for scope, value in (("email", normalize_email(email)), ("ip", ip)):
        attempt = _attempt(session, scope, value)
        if attempt and attempt.blocked_until and as_utc(attempt.blocked_until) > now:
            retry = max(retry, int((as_utc(attempt.blocked_until) - now).total_seconds()) + 1)
    return retry


def record_login_failure(session: Session, email: str, ip: str) -> int:
    now = utc_now()
    retry = 0
    for scope, value in (("email", normalize_email(email)), ("ip", ip)):
        attempt = _attempt(session, scope, value)
        if attempt is None:
            attempt = LoginAttempt(scope=scope, identifier_hash=identifier_hash(scope, value), failed_count=0)
            session.add(attempt)
        elif now - as_utc(attempt.last_failed_at) > timedelta(minutes=1):
            attempt.failed_count = 0
            attempt.first_failed_at = now
            attempt.blocked_until = None
        attempt.failed_count += 1
        attempt.last_failed_at = now
        if attempt.failed_count >= 6:
            delay = min(30 * (2 ** (attempt.failed_count - 6)), 1800)
            attempt.blocked_until = now + timedelta(seconds=delay)
            retry = max(retry, delay)
    session.flush()
    return retry


def clear_login_failures(session: Session, email: str, ip: str) -> None:
    pairs = (("email", normalize_email(email)), ("ip", ip))
    for scope, value in pairs:
        session.execute(
            delete(LoginAttempt).where(
                LoginAttempt.scope == scope,
                LoginAttempt.identifier_hash == identifier_hash(scope, value),
            )
        )


def add_security_event(
    session: Session,
    request: Request,
    event_type: str,
    user_id=None,
    metadata: dict | None = None,
) -> None:
    session.add(
        SecurityEvent(
            user_id=user_id,
            event_type=event_type,
            ip_address=client_ip(request),
            user_agent=user_agent(request),
            metadata_json=metadata or {},
        )
    )


def create_session(session: Session, request: Request, user: User, remember_me: bool) -> tuple[AuthSession, str, str]:
    settings = get_settings()
    raw_token = new_token()
    raw_csrf = new_token()
    days = settings.auth_remember_days if remember_me else settings.auth_session_days
    now = utc_now()
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        csrf_token_hash=token_hash(raw_csrf),
        ip_address=client_ip(request),
        user_agent=user_agent(request),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=days),
    )
    session.add(auth_session)
    session.flush()
    return auth_session, raw_token, raw_csrf


def set_auth_cookies(response: Response, raw_token: str, raw_csrf: str, expires_at: datetime) -> None:
    settings = get_settings()
    secure = settings.auth_cookie_secure or settings.is_production
    max_age = max(0, int((as_utc(expires_at) - utc_now()).total_seconds()))
    response.set_cookie(
        settings.auth_session_cookie_name,
        raw_token,
        max_age=max_age,
        expires=as_utc(expires_at),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        raw_csrf,
        max_age=max_age,
        expires=as_utc(expires_at),
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    secure = settings.auth_cookie_secure or settings.is_production
    response.delete_cookie(settings.auth_session_cookie_name, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/", secure=secure, httponly=False, samesite="lax")


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=get_settings().auth_totp_issuer)


def verify_totp(secret: str, code: str) -> bool:
    try:
        return bool(pyotp.TOTP(secret).verify(code, valid_window=1))
    except (TypeError, ValueError):
        return False
