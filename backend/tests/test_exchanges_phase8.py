import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import func, select

from app.connectors.bitget.client import BitgetApiClient
from app.connectors.bitget.sync import BitgetSyncService
from app.connectors.bybit.client import BybitApiClient
from app.connectors.bybit.sync import BybitSyncService
from app.core.config import Settings
from app.models import Account, AccountEquitySnapshot, AccountKind, ApiConnection, BalanceSnapshot, Portfolio, SyncRunStatus
from app.schemas import BitgetSyncRequest, BybitSyncRequest
from app.services.crypto import CredentialCipher

TEST_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def seed_connection(session, provider: str, *, passphrase: bool = False):
    portfolio = Portfolio(name=f"Phase8-{provider}-{uuid4().hex[:8]}", base_currency="USD")
    session.add(portfolio)
    session.flush()
    account = Account(portfolio_id=portfolio.id, kind=AccountKind.EXCHANGE, provider=provider, label=provider.title(), external_account_id="main")
    session.add(account)
    session.flush()
    cipher = CredentialCipher(TEST_KEY)
    connection = ApiConnection(
        account_id=account.id,
        name="read-only",
        provider=provider,
        encrypted_api_key=cipher.encrypt("public-key"),
        encrypted_api_secret=cipher.encrypt("private-secret"),
        encrypted_passphrase=cipher.encrypt("passphrase") if passphrase else None,
        requested_permissions=["read"],
    )
    session.add(connection)
    session.commit()
    return connection


def test_bybit_hmac_signature_never_places_secret_in_request():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {}})

    with BybitApiClient(
        api_key="public-key",
        api_secret="private-secret",
        base_url="https://bybit.test",
        transport=httpx.MockTransport(handler),
        now_ms=lambda: 1_700_000_000_000,
    ) as client:
        client.signed_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    request = seen[0]
    query = request.url.query.decode()
    expected = hmac.new(b"private-secret", f"1700000000000public-key5000{query}".encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected
    assert "private-secret" not in str(request.url)


def test_bitget_hmac_signature_includes_sorted_query_and_passphrase_header_only():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"code": "00000", "msg": "success", "data": []})

    with BitgetApiClient(
        api_key="public-key",
        api_secret="private-secret",
        passphrase="phrase",
        base_url="https://bitget.test",
        transport=httpx.MockTransport(handler),
        now_ms=lambda: 1_700_000_000_000,
    ) as client:
        client.signed_get("/api/v2/spot/account/assets", {"z": 2, "a": 1})
    request = seen[0]
    prehash = "1700000000000GET/api/v2/spot/account/assets?a=1&z=2"
    expected = base64.b64encode(hmac.new(b"private-secret", prehash.encode(), hashlib.sha256).digest()).decode()
    assert request.headers["ACCESS-SIGN"] == expected
    assert request.headers["ACCESS-PASSPHRASE"] == "phrase"
    assert "private-secret" not in str(request.url)


class FakeBybitClient:
    read_only = True

    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def signed_get(self, path, params=None):
        if path == "/v5/user/query-api":
            return {"retCode": 0, "result": {"readOnly": 1 if self.read_only else 0}}
        if path == "/v5/account/wallet-balance":
            return {"retCode": 0, "result": {"list": [{"accountType": "UNIFIED", "totalEquity": "1200", "totalAvailableBalance": "1100", "totalInitialMargin": "100", "totalPerpUPL": "20", "coin": [{"coin": "USDT", "walletBalance": "1000", "borrowAmount": "0"}]}]}}
        if path in {"/v5/asset/deposit/query-record", "/v5/asset/withdraw/query-record"}:
            return {"retCode": 0, "result": {"rows": [], "nextPageCursor": ""}}
        if path in {"/v5/account/transaction-log", "/v5/execution/list"}:
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        raise AssertionError((path, params))


class UnsafeBybitClient(FakeBybitClient):
    read_only = False


class FakeBitgetClient:
    unsafe = False

    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def signed_get(self, path, params=None):
        if path == "/api/v2/spot/account/info":
            return {"authorities": ["stow"] if self.unsafe else ["stor", "cpor"]}
        if path == "/api/v2/spot/account/assets":
            return [{"coin": "USDT", "available": "500", "frozen": "0", "locked": "0"}]
        if path in {"/api/v2/spot/wallet/deposit-records", "/api/v2/spot/wallet/withdrawal-records", "/api/v2/spot/trade/fills"}:
            return []
        raise AssertionError((path, params))


class UnsafeBitgetClient(FakeBitgetClient):
    unsafe = True


def test_bybit_read_only_sync_writes_balance_and_equity_and_rejects_write_key(db_session):
    connection = seed_connection(db_session, "bybit")
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)
    request = BybitSyncRequest(products=["spot"], history_start=end - timedelta(days=1), history_end=end)
    run = BybitSyncService(db_session, Settings(master_encryption_key=TEST_KEY), client_factory=FakeBybitClient).run(connection.id, request)
    assert run.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(BalanceSnapshot)) == 1
    assert db_session.scalar(select(func.count()).select_from(AccountEquitySnapshot)) == 1

    unsafe_connection = seed_connection(db_session, "bybit")
    rejected = BybitSyncService(db_session, Settings(master_encryption_key=TEST_KEY), client_factory=UnsafeBybitClient).run(unsafe_connection.id, request)
    assert rejected.status == SyncRunStatus.FAILED
    assert rejected.error_code == "BYBIT_UNSAFE_PERMISSIONS"


def test_bitget_read_only_sync_writes_balance_and_rejects_trade_permission(db_session):
    connection = seed_connection(db_session, "bitget", passphrase=True)
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)
    request = BitgetSyncRequest(products=["spot"], history_start=end - timedelta(days=1), history_end=end)
    run = BitgetSyncService(db_session, Settings(master_encryption_key=TEST_KEY), client_factory=FakeBitgetClient).run(connection.id, request)
    assert run.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(BalanceSnapshot)) == 1

    unsafe_connection = seed_connection(db_session, "bitget", passphrase=True)
    rejected = BitgetSyncService(db_session, Settings(master_encryption_key=TEST_KEY), client_factory=UnsafeBitgetClient).run(unsafe_connection.id, request)
    assert rejected.status == SyncRunStatus.FAILED
    assert rejected.error_code == "BITGET_UNSAFE_PERMISSIONS"
