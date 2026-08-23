from sqlalchemy import select
from uuid import UUID

from app.api.routes import evm as evm_routes
from app.core.config import Settings
from app.models import ApiConnection
from app.services.crypto import CredentialCipher


def _portfolio(client) -> dict:
    response = client.post("/api/v1/portfolios", json={"name": "Phase 8", "base_currency": "USD"})
    assert response.status_code == 201, response.text
    return response.json()


def test_binance_credentials_can_be_rotated_without_being_returned(client, db_session) -> None:
    portfolio = _portfolio(client)
    account = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "exchange",
            "provider": "binance",
            "label": "Binance Main",
        },
    )
    assert account.status_code == 201, account.text
    connection = client.post(
        "/api/v1/connections",
        json={
            "account_id": account.json()["id"],
            "name": "read-only",
            "provider": "binance",
            "api_key": "old-key",
            "api_secret": "old-secret",
            "requested_permissions": ["read"],
        },
    )
    assert connection.status_code == 201, connection.text

    rotated = client.patch(
        f"/api/v1/connections/{connection.json()['id']}",
        json={"api_key": "new-key", "api_secret": "new-secret"},
    )

    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["api_key_hint"] == "••••"
    assert "api_key" not in rotated.json()
    stored = db_session.scalar(select(ApiConnection).where(ApiConnection.id == UUID(connection.json()["id"])))
    cipher = CredentialCipher(Settings().master_encryption_key)
    assert cipher.decrypt(stored.encrypted_api_key) == "new-key"
    assert cipher.decrypt(stored.encrypted_api_secret) == "new-secret"


def test_hyperliquid_rotation_rejects_secrets(client) -> None:
    portfolio = _portfolio(client)
    address = "0x1111111111111111111111111111111111111111"
    account = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "perp_dex",
            "provider": "hyperliquid",
            "label": "Hyperliquid",
            "external_account_id": address,
            "address": address,
        },
    ).json()
    connection = client.post(
        "/api/v1/connections",
        json={
            "account_id": account["id"],
            "name": "public-address",
            "provider": "hyperliquid",
            "api_key": address,
            "requested_permissions": ["read"],
        },
    ).json()

    rejected = client.patch(
        f"/api/v1/connections/{connection['id']}",
        json={"api_secret": "must-not-be-accepted"},
    )

    assert rejected.status_code == 422
    assert "public wallet address only" in rejected.json()["detail"]


def test_evm_chain_capabilities_do_not_expose_rpc_urls(client, monkeypatch) -> None:
    monkeypatch.setattr(
        evm_routes,
        "get_settings",
        lambda: Settings(_env_file=None, evm_base_rpc_url="https://private-rpc.example"),
    )

    response = client.get("/api/v1/evm/chains")

    assert response.status_code == 200, response.text
    chains = response.json()
    assert next(chain for chain in chains if chain["key"] == "base")["configured"] is True
    assert next(chain for chain in chains if chain["key"] == "ethereum")["configured"] is False
    assert "private-rpc" not in response.text
