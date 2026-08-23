import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("AUTH_BOOTSTRAP_TOKEN", "test-bootstrap-token")

from app.db import Base, get_session  # noqa: E402
import app.models  # noqa: E402,F401
from app.main import app  # noqa: E402


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def raw_client(db_session):

    def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def client(raw_client):
    password = "Correct-Horse-Battery-Staple-2026"
    registered = raw_client.post(
        "/api/v1/auth/register",
        json={"email": "owner@example.com", "password": password},
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
    )
    assert registered.status_code == 201, registered.text
    raw_client.headers["X-CSRF-Token"] = raw_client.cookies.get("bags_csrf")
    elevated = raw_client.post(
        "/api/v1/auth/sensitive/verify",
        json={"current_password": password},
    )
    assert elevated.status_code == 200, elevated.text
    yield raw_client
