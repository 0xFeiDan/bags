from datetime import timedelta

import pyotp
from sqlalchemy import select

from app.models import AuthSession, User
from app.services.security import utc_now

EMAIL = "security@example.com"
PASSWORD = "A-very-long-test-password-2026"


def register(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": EMAIL, "password": PASSWORD},
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = client.cookies.get("bags_csrf")
    return response


def test_protected_api_rejects_unauthenticated_requests(raw_client):
    response = raw_client.get("/api/v1/portfolios")
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_registration_duplicate_and_login(raw_client):
    register(raw_client)
    duplicate = raw_client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert duplicate.status_code in {403, 409}

    logout = raw_client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    raw_client.headers.pop("X-CSRF-Token", None)
    wrong = raw_client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "wrong-password"})
    assert wrong.status_code == 401
    assert "邮箱、密码或验证码" in wrong.json()["detail"]
    correct = raw_client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert correct.status_code == 200
    assert correct.json()["authenticated"] is True


def test_login_rate_limit_blocks_the_sixth_failure(raw_client):
    register(raw_client)
    assert raw_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": raw_client.cookies.get("bags_csrf")}).status_code == 200
    raw_client.headers.pop("X-CSRF-Token", None)
    for _ in range(5):
        response = raw_client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "not-the-password"})
        assert response.status_code == 401
    blocked = raw_client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "not-the-password"})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 30


def test_totp_setup_requires_confirmation_and_login_challenge(raw_client, db_session):
    register(raw_client)
    not_elevated = raw_client.post("/api/v1/auth/totp/setup")
    assert not_elevated.status_code == 403
    elevated = raw_client.post(
        "/api/v1/auth/sensitive/verify",
        json={"current_password": PASSWORD},
    )
    assert elevated.status_code == 200
    setup = raw_client.post("/api/v1/auth/totp/setup")
    assert setup.status_code == 200
    secret = setup.json()["secret"]

    wrong = raw_client.post("/api/v1/auth/totp/confirm", json={"code": "000000"})
    assert wrong.status_code == 422
    code = pyotp.TOTP(secret).now()
    confirmed = raw_client.post("/api/v1/auth/totp/confirm", json={"code": code})
    assert confirmed.status_code == 200
    user = db_session.scalar(select(User).where(User.email == EMAIL))
    assert user.two_factor_enabled is True
    assert secret not in user.totp_secret_encrypted

    assert raw_client.post("/api/v1/auth/logout").status_code == 200
    raw_client.headers.pop("X-CSRF-Token", None)
    first_step = raw_client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert first_step.status_code == 200
    assert first_step.json()["totp_required"] is True
    challenge = first_step.json()["challenge"]
    invalid = raw_client.post("/api/v1/auth/login/totp", json={"challenge": challenge, "code": "000000"})
    assert invalid.status_code == 401
    completed = raw_client.post(
        "/api/v1/auth/login/totp",
        json={"challenge": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert completed.status_code == 200
    assert completed.json()["authenticated"] is True


def test_session_expiry_logout_and_csrf(raw_client, db_session):
    register(raw_client)
    no_csrf = raw_client.post("/api/v1/portfolios", json={"name": "No CSRF"}, headers={"X-CSRF-Token": "bad"})
    assert no_csrf.status_code == 403
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.revoked_at.is_(None)))
    auth_session.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    expired = raw_client.get("/api/v1/portfolios")
    assert expired.status_code == 401


def test_sensitive_authorization_window(raw_client):
    register(raw_client)
    raw_client.headers["X-CSRF-Token"] = raw_client.cookies.get("bags_csrf")
    denied = raw_client.post("/api/v1/auth/password", json={"new_password": "Another-long-password-2026"})
    assert denied.status_code == 403
    wrong = raw_client.post("/api/v1/auth/sensitive/verify", json={"current_password": "wrong"})
    assert wrong.status_code == 401
    verified = raw_client.post("/api/v1/auth/sensitive/verify", json={"current_password": PASSWORD})
    assert verified.status_code == 200
    changed = raw_client.post("/api/v1/auth/password", json={"new_password": "Another-long-password-2026"})
    assert changed.status_code == 200
