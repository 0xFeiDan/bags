import hmac
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_session
from app.models import AuthSession, User
from app.services.security import as_utc, token_hash, utc_now

PUBLIC_PATHS = {
    "/health",
    "/ready",
    "/auth/bootstrap-status",
    "/auth/register",
    "/auth/login",
    "/auth/login/totp",
}


def require_authenticated_request(request: Request, session: Session = Depends(get_session)) -> None:
    settings = get_settings()
    relative_path = request.url.path.removeprefix(settings.api_v1_prefix)
    if request.method == "OPTIONS" or relative_path in PUBLIC_PATHS:
        return

    raw_token = request.cookies.get(settings.auth_session_cookie_name)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw_token)))
    now = utc_now()
    if not auth_session or auth_session.revoked_at or as_utc(auth_session.expires_at) <= now:
        if auth_session and not auth_session.revoked_at:
            auth_session.revoked_at = now
            session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期，请重新登录")
    user = session.get(User, auth_session.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf_cookie = request.cookies.get(settings.auth_csrf_cookie_name, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        valid_pair = bool(csrf_cookie and csrf_header and hmac.compare_digest(csrf_cookie, csrf_header))
        valid_hash = bool(csrf_cookie and hmac.compare_digest(token_hash(csrf_cookie), auth_session.csrf_token_hash))
        if not valid_pair or not valid_hash:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="安全校验失败，请刷新页面后重试")

    request.state.user = user
    request.state.auth_session = auth_session
    if now - as_utc(auth_session.last_seen_at) >= timedelta(minutes=5):
        auth_session.last_seen_at = now
        session.commit()


def require_recent_sensitive_auth(request: Request) -> None:
    auth_session: AuthSession | None = getattr(request.state, "auth_session", None)
    if not auth_session or not auth_session.last_sensitive_auth_at:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请先验证当前密码")
    if utc_now() - as_utc(auth_session.last_sensitive_auth_at) > timedelta(minutes=get_settings().auth_sensitive_minutes):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="安全验证已过期，请重新验证")
