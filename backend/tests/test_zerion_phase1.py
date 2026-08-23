from app.core.config import get_settings


def create_evm_account(client):
    portfolio = client.post("/api/v1/portfolios", json={"name": "Zerion portfolio", "base_currency": "USD"}).json()
    response = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "wallet",
            "provider": "evm",
            "label": "Zerion EVM wallet",
            "chain_id": "8453",
            "address": "0x1111111111111111111111111111111111111111",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_phase1_registers_disabled_zerion_source_without_external_configuration(client):
    account = create_evm_account(client)

    status = client.get("/api/v1/zerion/status")
    assert status.status_code == 200, status.text
    assert status.json() == {
        "configured": False,
        "requests_per_second_limit": 3,
        "daily_request_limit": 2000,
        "daily_request_budget": 1800,
        "max_requests_per_run": 3,
        "min_sync_interval_seconds": 900,
    }

    configured = client.put(
        f"/api/v1/zerion/accounts/{account['id']}/source",
        json={"is_enabled": False, "mode": "disabled"},
    )

    assert configured.status_code == 200, configured.text
    body = configured.json()
    assert body["provider"] == "zerion"
    assert body["mode"] == "disabled"
    assert body["is_enabled"] is False
    assert body["zerion_configured"] is False
    assert body["requests_per_second_limit"] == 3
    assert body["daily_request_limit"] == 2000
    assert body["max_requests_per_run"] == 3
    assert body["min_sync_interval_seconds"] == 900
    assert body["daily_request_budget"] == 1800

    listed = client.get(f"/api/v1/zerion/accounts/{account['id']}/sync-runs")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


def test_phase1_refuses_enabled_source_without_server_api_key(client):
    account = create_evm_account(client)

    response = client.put(
        f"/api/v1/zerion/accounts/{account['id']}/source",
        json={"is_enabled": True, "mode": "shadow"},
    )

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_phase1_accepts_only_shadow_and_preserves_free_plan_caps(client, monkeypatch):
    account = create_evm_account(client)
    monkeypatch.setenv("ZERION_ENABLED", "true")
    monkeypatch.setenv("ZERION_API_KEY", "test-key")
    monkeypatch.setenv("ZERION_REQUESTS_PER_SECOND_LIMIT", "999")
    monkeypatch.setenv("ZERION_DAILY_REQUEST_LIMIT", "999999")
    monkeypatch.setenv("ZERION_MAX_REQUESTS_PER_RUN", "999")
    monkeypatch.setenv("ZERION_MIN_SYNC_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("ZERION_DAILY_REQUEST_BUDGET", "9999")
    get_settings.cache_clear()
    try:
        active = client.put(
            f"/api/v1/zerion/accounts/{account['id']}/source",
            json={"is_enabled": True, "mode": "active"},
        )
        assert active.status_code == 422

        shadow = client.put(
            f"/api/v1/zerion/accounts/{account['id']}/source",
            json={"is_enabled": True, "mode": "shadow"},
        )
        assert shadow.status_code == 200, shadow.text
        body = shadow.json()
        assert body["zerion_configured"] is True
        assert body["mode"] == "shadow"
        assert body["requests_per_second_limit"] == 3
        assert body["daily_request_limit"] == 2000
        assert body["max_requests_per_run"] == 3
        assert body["min_sync_interval_seconds"] == 900
        assert body["daily_request_budget"] == 1800

        status_response = client.get("/api/v1/zerion/status")
        assert status_response.status_code == 200
        assert status_response.json()["configured"] is True
    finally:
        get_settings.cache_clear()
