import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from app.connectors.perp_dex.hyperliquid.client import HyperliquidClient
from app.connectors.perp_dex.hyperliquid.collector import HyperliquidCollector
from app.connectors.perp_dex.hyperliquid.sync import HyperliquidSyncService
from app.core.config import Settings
from app.models import (
    Account,
    AccountEquitySnapshot,
    AccountKind,
    ApiConnection,
    BalanceSnapshot,
    LedgerEntry,
    LedgerEvent,
    Portfolio,
    PositionSnapshot,
    RawEvent,
    SyncRunStatus,
)
from app.schemas import HyperliquidSyncRequest
from app.services.crypto import CredentialCipher

TEST_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
ADDRESS = "0x1111111111111111111111111111111111111111"


def test_hyperliquid_client_is_restricted_to_public_info_reads():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"assetPositions": [], "marginSummary": {}})

    client = HyperliquidClient(base_url="https://hyperliquid.test", transport=httpx.MockTransport(handler))
    try:
        client.info({"type": "clearinghouseState", "user": ADDRESS})
        try:
            client.info({"type": "withdraw", "user": ADDRESS})
            raise AssertionError("mutable operation should have been refused")
        except ValueError:
            pass
    finally:
        client.close()

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/info"
    assert json.loads(seen[0].content)["type"] == "clearinghouseState"


class FakeHyperliquidClient:
    calls: list[dict] = []

    def __init__(self, **_: object) -> None:
        type(self).calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def info(self, payload):
        payload = dict(payload)
        type(self).calls.append(payload)
        request_type = payload["type"]
        if request_type == "clearinghouseState":
            return {
                "assetPositions": [
                    {
                        "type": "oneWay",
                        "position": {
                            "coin": "BTC",
                            "entryPx": "49000",
                            "leverage": {"type": "cross", "value": 20},
                            "liquidationPx": "30000",
                            "marginUsed": "1250",
                            "positionValue": "25000",
                            "returnOnEquity": "0.4",
                            "szi": "0.5",
                            "unrealizedPnl": "500",
                        },
                    }
                ],
                "marginSummary": {
                    "accountValue": "1000",
                    "totalMarginUsed": "1250",
                    "totalNtlPos": "25000",
                    "totalRawUsd": "800",
                },
                "withdrawable": "700",
            }
        if request_type == "metaAndAssetCtxs":
            return [[{"name": "BTC", "szDecimals": 5}], [{"markPx": "50000"}]]
        if request_type == "spotClearinghouseState":
            return {
                "balances": [
                    {"coin": "USDC", "token": 0, "total": "100", "hold": "0", "entryNtl": "100"},
                    {"coin": "HYPE", "token": 1, "total": "2", "hold": "0", "entryNtl": "40"},
                ]
            }
        if request_type == "spotMeta":
            return {
                "tokens": [
                    {"name": "USDC", "index": 0, "szDecimals": 8, "weiDecimals": 8},
                    {"name": "HYPE", "index": 1, "szDecimals": 2, "weiDecimals": 8},
                ],
                "universe": [],
            }
        if request_type == "userFillsByTime":
            return [
                {
                    "closedPnl": "25",
                    "coin": "BTC",
                    "crossed": True,
                    "dir": "Close Long",
                    "hash": "0xfill",
                    "oid": 101,
                    "tid": 1001,
                    "px": "50000",
                    "side": "A",
                    "startPosition": "1",
                    "sz": "0.5",
                    "time": 1_700_000_100_000,
                    "fee": "0.5",
                    "feeToken": "USDC",
                }
            ]
        if request_type == "userFunding":
            return [
                {
                    "time": 1_700_000_200_000,
                    "hash": "0xfunding",
                    "delta": {"type": "funding", "coin": "BTC", "usdc": "-1.25", "szi": "0.5", "fundingRate": "0.0001"},
                }
            ]
        if request_type == "userNonFundingLedgerUpdates":
            return [
                {"time": 1_700_000_300_000, "hash": "0xdeposit", "delta": {"type": "deposit", "usdc": "100"}},
                {"time": 1_700_000_400_000, "hash": "0xwithdraw", "delta": {"type": "withdraw", "usdc": "40", "fee": "1"}},
                {
                    "time": 1_700_000_500_000,
                    "hash": "0xtransfer",
                    "delta": {"type": "internalTransfer", "usdc": "10", "user": ADDRESS, "destination": "0x2222222222222222222222222222222222222222", "fee": "0.1"},
                },
                {"time": 1_700_000_600_000, "hash": "0xclass", "delta": {"type": "accountClassTransfer", "usdc": "20"}},
            ]
        raise AssertionError(payload)


def test_hyperliquid_full_same_millisecond_page_is_marked_incomplete_not_skipped():
    class FullPageClient:
        calls = 0

        def info(self, payload):
            assert payload["type"] == "userFillsByTime"
            type(self).calls += 1
            return [{"time": 1_700_000_000_000, "tid": index} for index in range(HyperliquidCollector.PAGE_SIZE)]

    result = HyperliquidCollector(FullPageClient()).fills(
        ADDRESS,
        datetime(2023, 11, 14, tzinfo=timezone.utc),
        datetime(2023, 11, 15, tzinfo=timezone.utc),
    )
    assert result.truncated is True
    assert len(result.records) == HyperliquidCollector.PAGE_SIZE
    assert FullPageClient.calls == 2


def seed_connection(session):
    portfolio = Portfolio(name="Hyperliquid Phase3", base_currency="USD")
    session.add(portfolio)
    session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.PERP_DEX,
        provider="hyperliquid",
        label="Hyperliquid",
        external_account_id=ADDRESS,
        address=ADDRESS,
    )
    session.add(account)
    session.flush()
    cipher = CredentialCipher(TEST_KEY)
    connection = ApiConnection(
        account_id=account.id,
        name="public-wallet",
        provider="hyperliquid",
        encrypted_api_key=cipher.encrypt(ADDRESS),
        requested_permissions=["read"],
    )
    session.add(connection)
    session.commit()
    return account, connection


def test_hyperliquid_sync_normalizes_equity_positions_history_and_is_idempotent(db_session):
    account, connection = seed_connection(db_session)
    settings = Settings(master_encryption_key=TEST_KEY)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    request = HyperliquidSyncRequest(history_start=end - timedelta(days=1), history_end=end)

    first = HyperliquidSyncService(db_session, settings, client_factory=FakeHyperliquidClient).run(connection.id, request)
    assert first.status == SyncRunStatus.SUCCEEDED, (first.error_code, first.error_message)
    assert first.stats_json == {
        "raw_created": 10,
        "raw_existing": 0,
        "ledger_created": 6,
        "balances_created": 2,
        "positions_created": 1,
        "equity_created": 1,
    }
    equity = db_session.scalar(select(AccountEquitySnapshot).where(AccountEquitySnapshot.account_id == account.id))
    assert equity.equity == 1000
    assert equity.unrealized_pnl == 500
    position = db_session.scalar(select(PositionSnapshot).where(PositionSnapshot.account_id == account.id))
    assert position.symbol == "BTC"
    assert position.position_side == "LONG"
    assert position.mark_price == 50000
    assert db_session.scalar(select(func.count()).select_from(BalanceSnapshot)) == 2
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 6
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 10
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.fee_flag.is_(True))) == 3

    raw_count = db_session.scalar(select(func.count()).select_from(RawEvent))
    ledger_count = db_session.scalar(select(func.count()).select_from(LedgerEvent))
    second = HyperliquidSyncService(db_session, settings, client_factory=FakeHyperliquidClient).run(connection.id, request)
    assert second.status == SyncRunStatus.SUCCEEDED
    assert second.stats_json["raw_created"] == 0
    assert second.stats_json["raw_existing"] == raw_count
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == raw_count
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == ledger_count


def test_public_hyperliquid_connection_uses_account_address_without_api_key(client):
    portfolio = client.post("/api/v1/portfolios", json={"name": "HL Public", "base_currency": "USD"}).json()
    account_response = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "perp_dex",
            "provider": "hyperliquid",
            "label": "Hyperliquid",
            "external_account_id": ADDRESS,
            "address": ADDRESS,
        },
    )
    assert account_response.status_code == 201, account_response.text
    connection = client.post(
        "/api/v1/connections",
        json={
            "account_id": account_response.json()["id"],
            "name": "public-wallet",
            "provider": "hyperliquid",
            "requested_permissions": ["read"],
        },
    )
    assert connection.status_code == 201, connection.text
    assert ADDRESS not in connection.text
    rejected_secret = client.post(
        "/api/v1/connections",
        json={
            "account_id": account_response.json()["id"],
            "name": "not-a-public-address",
            "provider": "hyperliquid",
            "api_key": "this-must-not-be-a-private-key",
            "requested_permissions": ["read"],
        },
    )
    assert rejected_secret.status_code == 422
    rejected_private_material = client.post(
        "/api/v1/connections",
        json={
            "account_id": account_response.json()["id"],
            "name": "secret-material",
            "provider": "hyperliquid",
            "api_key": ADDRESS,
            "api_secret": "wallet-private-key-must-not-be-accepted",
            "requested_permissions": ["read"],
        },
    )
    assert rejected_private_material.status_code == 422
