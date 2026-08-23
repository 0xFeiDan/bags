from datetime import datetime, timezone


def _portfolio(client, name: str) -> dict:
    response = client.post("/api/v1/portfolios", json={"name": name, "base_currency": "USD"})
    assert response.status_code == 201, response.text
    return response.json()


def _asset(client) -> dict:
    response = client.post(
        "/api/v1/assets",
        json={"canonical_symbol": "BTC", "name": "Bitcoin", "asset_type": "native", "decimals": 8},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _account(client, portfolio_id: str, identity: str) -> dict:
    response = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio_id,
            "kind": "exchange",
            "provider": "binance",
            "label": identity,
            "external_account_id": identity,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_bootstrap_requires_token(raw_client):
    denied = raw_client.post(
        "/api/v1/auth/register",
        json={"email": "owner@example.com", "password": "Correct-Horse-Battery-Staple-2026"},
    )
    assert denied.status_code == 403
    created = raw_client.post(
        "/api/v1/auth/register",
        json={"email": "owner@example.com", "password": "Correct-Horse-Battery-Staple-2026"},
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
    )
    assert created.status_code == 201


def test_ledger_event_rejects_foreign_portfolio_account(client):
    first = _portfolio(client, "First")
    second = _portfolio(client, "Second")
    asset = _asset(client)
    foreign_account = _account(client, second["id"], "foreign-account")
    response = client.post(
        "/api/v1/ledger/events",
        json={
            "portfolio_id": first["id"],
            "event_type": "deposit",
            "source": "manual",
            "status": "posted",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "entries": [{"account_id": foreign_account["id"], "asset_id": asset["id"], "direction": "credit", "quantity": "1"}],
        },
    )
    assert response.status_code == 422
    assert "event portfolio" in response.json()["detail"]


def test_raw_event_rejects_connection_from_another_account(client):
    portfolio = _portfolio(client, "Personal")
    first = _account(client, portfolio["id"], "first-account")
    second = _account(client, portfolio["id"], "second-account")
    connection = client.post(
        "/api/v1/connections",
        json={"account_id": second["id"], "name": "second-connection", "provider": "binance", "api_key": "test-key"},
    )
    assert connection.status_code == 201, connection.text
    response = client.post(
        "/api/v1/raw-events",
        json={
            "account_id": first["id"],
            "connection_id": connection.json()["id"],
            "source": "manual",
            "external_event_id": "cross-account",
            "event_kind": "manual",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload_json": {},
        },
    )
    assert response.status_code == 422


def test_raw_event_payload_is_bounded(client):
    portfolio = _portfolio(client, "Personal")
    account = _account(client, portfolio["id"], "bounded-account")
    response = client.post(
        "/api/v1/raw-events",
        json={
            "account_id": account["id"],
            "source": "manual",
            "external_event_id": "oversized",
            "event_kind": "manual",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload_json": {"payload": "x" * (256 * 1024)},
        },
    )
    assert response.status_code == 422


def test_asset_identity_cannot_be_created_twice(client):
    _asset(client)
    duplicate = client.post(
        "/api/v1/assets",
        json={"canonical_symbol": "BTC", "name": "Bitcoin duplicate", "asset_type": "native", "decimals": 8},
    )
    assert duplicate.status_code == 409
