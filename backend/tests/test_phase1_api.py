from datetime import datetime, timezone


def create_foundation(client):
    portfolio = client.post("/api/v1/portfolios", json={"name": "Personal", "base_currency": "USD"})
    assert portfolio.status_code == 201
    asset = client.post(
        "/api/v1/assets", json={"canonical_symbol": "BTC", "name": "Bitcoin", "asset_type": "native", "decimals": 8}
    )
    assert asset.status_code == 201
    account = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio.json()["id"],
            "kind": "exchange",
            "provider": "binance",
            "label": "Binance Spot",
            "external_account_id": "spot",
        },
    )
    assert account.status_code == 201
    return portfolio.json(), asset.json(), account.json()


def test_health_and_foundation_flow(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    portfolio, asset, account = create_foundation(client)
    event = client.post(
        "/api/v1/ledger/events",
        json={
            "portfolio_id": portfolio["id"],
            "event_type": "buy",
            "source": "manual",
            "status": "pending",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "entries": [
                {
                    "account_id": account["id"],
                    "asset_id": asset["id"],
                    "direction": "credit",
                    "quantity": "0.25",
                    "unit_price_usd": "100000",
                }
            ],
        },
    )
    assert event.status_code == 201, event.text
    assert event.json()["entries"][0]["quantity"] == "0.250000000000000000"


def test_portfolio_rejects_unsupported_base_currency(client):
    response = client.post("/api/v1/portfolios", json={"name": "EUR Portfolio", "base_currency": "EUR"})
    assert response.status_code == 422
    assert "USD" in response.text


def test_raw_events_are_deduplicated_and_not_mutable(client):
    _, _, account = create_foundation(client)
    raw = {
        "account_id": account["id"],
        "source": "binance",
        "external_event_id": "trade-123",
        "event_kind": "spot_trade",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload_json": {"orderId": "123", "symbol": "BTCUSDT"},
    }
    first = client.post("/api/v1/raw-events", json=raw)
    assert first.status_code == 201
    duplicate = client.post("/api/v1/raw-events", json=raw)
    assert duplicate.status_code == 409
    # There is deliberately no item mutation route for source evidence.
    assert client.put(f"/api/v1/raw-events/{first.json()['id']}", json={}).status_code == 404


def test_credentials_are_masked_and_withdraw_is_rejected(client):
    _, _, account = create_foundation(client)
    rejected = client.post(
        "/api/v1/connections",
        json={
            "account_id": account["id"],
            "name": "unsafe",
            "provider": "binance",
            "api_key": "secret",
            "requested_permissions": ["read", "withdraw"],
        },
    )
    assert rejected.status_code == 422
    created = client.post(
        "/api/v1/connections",
        json={
            "account_id": account["id"],
            "name": "read-only",
            "provider": "binance",
            "api_key": "abcd1234",
            "requested_permissions": ["read"],
        },
    )
    assert created.status_code == 201, created.text
    assert "abcd1234" not in created.text
    assert "encrypted" not in created.text

    private_key = client.post(
        "/api/v1/connections",
        json={
            "account_id": account["id"],
            "name": "must-not-accept",
            "provider": "binance",
            "api_key": "public-api-key",
            "private_key": "never-store-wallet-secrets",
            "requested_permissions": ["read"],
        },
    )
    assert private_key.status_code == 422
