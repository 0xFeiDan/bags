import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
from sqlalchemy import func, select

from app.connectors.binance.client import BinanceApiClient
from app.connectors.binance.collector import BinanceCollector
from app.connectors.binance.sync import BinanceSyncService
from app.core.config import Settings
from app.models import (
    Account,
    AccountKind,
    ApiConnection,
    BalanceSnapshot,
    ConnectionMarketScope,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    Portfolio,
    PositionSnapshot,
    RawEvent,
    SyncRunStatus,
)
from app.schemas import BinanceSyncRequest
from app.services.crypto import CredentialCipher

TEST_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def test_hmac_client_signs_get_and_never_places_secret_in_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        return httpx.Response(200, json={"balances": []})

    client = BinanceApiClient(
        api_key="public-key",
        api_secret="private-secret",
        base_urls={"spot": "https://spot.test", "usdm": "https://usdm.test", "coinm": "https://coinm.test"},
        transport=httpx.MockTransport(handler),
        now_ms=lambda: 1_700_000_000_000,
    )
    try:
        client.signed_get("spot", "/api/v3/account", {"omitZeroBalances": "true"})
    finally:
        client.close()

    request = seen[-1]
    query = request.url.query.decode()
    unsigned, signature = query.rsplit("&signature=", 1)
    expected = hmac.new(b"private-secret", unsigned.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert request.headers["X-MBX-APIKEY"] == "public-key"
    assert "private-secret" not in str(request.url)
    assert parse_qs(unsigned)["recvWindow"] == ["5000"]


class FakeBinanceClient:
    calls: list[tuple[str, str, dict]] = []

    def __init__(self, **_: object) -> None:
        type(self).calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def public_get(self, product: str, path: str, params=None):
        type(self).calls.append((product, path, dict(params or {})))
        if path.endswith("exchangeInfo"):
            if product == "spot":
                return {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"}]}
            if product == "usdm":
                return {"symbols": [{"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "marginAsset": "USDT"}]}
            if product == "coinm":
                return {"symbols": [{"symbol": "BTCUSD_PERP", "pair": "BTCUSD", "baseAsset": "BTC", "quoteAsset": "USD", "marginAsset": "BTC", "contractSize": 100}]}
            return {"symbols": []}
        raise AssertionError(path)

    def signed_get(self, product: str, path: str, params=None):
        params = dict(params or {})
        type(self).calls.append((product, path, params))
        if path == "/sapi/v1/account/apiRestrictions":
            return {"enableReading": True, "enableWithdrawals": False, "enableSpotAndMarginTrading": False}
        if path == "/api/v3/account":
            return {"balances": [{"asset": "BTC", "free": "1", "locked": "0"}, {"asset": "USDT", "free": "1000", "locked": "0"}]}
        if path == "/sapi/v1/capital/deposit/hisrec":
            return [{"id": "dep-1", "amount": "0.1", "coin": "BTC", "network": "BTC", "status": 1, "txId": "tx-dep", "insertTime": 1_700_000_000_000}]
        if path == "/sapi/v1/capital/withdraw/history":
            return [{"id": "wd-1", "amount": "0.2", "transactionFee": "0.001", "coin": "BTC", "network": "BTC", "status": 6, "txId": "tx-wd", "completeTime": "2023-11-14 22:20:00"}]
        if path == "/api/v3/myTrades":
            return [{"symbol": "BTCUSDT", "id": 11, "orderId": 22, "price": "40000", "qty": "0.01", "quoteQty": "400", "commission": "0.00001", "commissionAsset": "BTC", "time": 1_700_000_100_000, "isBuyer": True, "isMaker": False}]
        if path == "/fapi/v3/account":
            return {"assets": [{"asset": "USDT", "walletBalance": "500"}]}
        if path == "/dapi/v1/account":
            return {"assets": [{"asset": "BTC", "walletBalance": "0.5"}]}
        if path == "/fapi/v3/positionRisk":
            return [{"symbol": "ETHUSDT", "positionAmt": "2", "positionSide": "LONG", "entryPrice": "2000", "markPrice": "2100", "unRealizedProfit": "200", "leverage": "3", "liquidationPrice": "1000", "notional": "4200", "marginAsset": "USDT", "isolated": False}]
        if path == "/dapi/v1/positionRisk":
            return [{"symbol": "BTCUSD_PERP", "pair": "BTCUSD", "positionAmt": "10", "positionSide": "LONG", "entryPrice": "50000", "markPrice": "51000", "unRealizedProfit": "0.001", "leverage": "2", "liquidationPrice": "25000", "marginAsset": "BTC", "isolated": False}]
        if path == "/fapi/v1/userTrades":
            assert params.get("symbol") == "ETHUSDT"
            return [{"symbol": "ETHUSDT", "id": 31, "orderId": 32, "side": "BUY", "price": "2000", "qty": "2", "commission": "0.8", "commissionAsset": "USDT", "time": 1_700_000_200_000, "positionSide": "LONG"}]
        if path == "/dapi/v1/userTrades":
            assert params.get("pair") == "BTCUSD"
            return [{"symbol": "BTCUSD_PERP", "pair": "BTCUSD", "id": 41, "orderId": 42, "side": "BUY", "price": "50000", "qty": "10", "commission": "0.00002", "commissionAsset": "BTC", "time": 1_700_000_300_000, "positionSide": "LONG"}]
        if path == "/fapi/v1/income":
            return [{"symbol": "ETHUSDT", "incomeType": "FUNDING_FEE", "income": "-1", "asset": "USDT", "time": 1_700_000_400_000, "tranId": 51, "tradeId": ""}, {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "5", "asset": "USDT", "time": 1_700_000_500_000, "tranId": 52, "tradeId": "31"}]
        if path == "/dapi/v1/income":
            return [{"symbol": "BTCUSD_PERP", "incomeType": "FUNDING_FEE", "income": "0.0001", "asset": "BTC", "time": 1_700_000_600_000, "tranId": "61", "tradeId": ""}, {"symbol": "BTCUSD_PERP", "incomeType": "REALIZED_PNL", "income": "0.0002", "asset": "BTC", "time": 1_700_000_700_000, "tranId": "62", "tradeId": "41"}]
        raise AssertionError((product, path, params))


class UnsafeBinanceClient(FakeBinanceClient):
    def signed_get(self, product: str, path: str, params=None):
        if path == "/sapi/v1/account/apiRestrictions":
            return {"enableReading": True, "enableWithdrawals": True}
        return super().signed_get(product, path, params)


class TradingBinanceClient(FakeBinanceClient):
    def signed_get(self, product: str, path: str, params=None):
        if path == "/sapi/v1/account/apiRestrictions":
            return {"enableReading": True, "enableWithdrawals": False, "enableSpotAndMarginTrading": True}
        return super().signed_get(product, path, params)


class PendingThenCompletedBinanceClient(FakeBinanceClient):
    completed = False

    def signed_get(self, product: str, path: str, params=None):
        response = super().signed_get(product, path, params)
        if not type(self).completed and path == "/sapi/v1/capital/deposit/hisrec":
            return [{**item, "status": 0} for item in response]
        if not type(self).completed and path == "/sapi/v1/capital/withdraw/history":
            return [{**item, "status": 4} for item in response]
        return response


class SameMillisecondTradeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def signed_get(self, _product: str, _path: str, params=None):
        params = dict(params or {})
        self.calls.append(params)
        if "fromId" not in params:
            return [{"id": index, "time": 1_700_000_000_000} for index in range(1, 1001)]
        assert params["fromId"] == 1001
        return [{"id": 1001, "time": 1_700_000_000_000}]


def seed_connection(session):
    portfolio = Portfolio(name="Phase2", base_currency="USD")
    session.add(portfolio)
    session.flush()
    account = Account(portfolio_id=portfolio.id, kind=AccountKind.EXCHANGE, provider="binance", label="Binance", external_account_id="main")
    session.add(account)
    session.flush()
    cipher = CredentialCipher(TEST_KEY)
    connection = ApiConnection(
        account_id=account.id,
        name="read-only",
        provider="binance",
        encrypted_api_key=cipher.encrypt("key"),
        encrypted_api_secret=cipher.encrypt("secret"),
        requested_permissions=["read"],
    )
    session.add(connection)
    session.commit()
    return connection


def test_all_binance_products_sync_and_second_run_is_idempotent(db_session, client):
    connection = seed_connection(db_session)
    settings = Settings(master_encryption_key=TEST_KEY)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    request = BinanceSyncRequest(
        products=["spot", "usdm", "coinm"],
        spot_symbols=["BTCUSDT"],
        usdm_symbols=["ETHUSDT"],
        coinm_pairs=["BTCUSD"],
        history_start=end - timedelta(days=1),
        history_end=end,
    )
    first = BinanceSyncService(db_session, settings, client_factory=FakeBinanceClient).run(connection.id, request)
    assert first.status == SyncRunStatus.SUCCEEDED
    assert first.stats_json == {
        "raw_created": 18,
        "raw_existing": 0,
        "ledger_created": 9,
        "balances_created": 4,
        "positions_created": 2,
        "spot_symbols_discovered": 1,
        "spot_symbols_synced": 1,
    }
    assert db_session.scalar(select(func.count()).select_from(Account)) == 3
    assert db_session.scalar(select(func.count()).select_from(BalanceSnapshot)) == 4
    assert db_session.scalar(select(func.count()).select_from(PositionSnapshot)) == 2
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 9
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.fee_flag.is_(True))) == 4
    visible_accounts = client.get("/api/v1/accounts")
    assert visible_accounts.status_code == 200
    assert [item["label"] for item in visible_accounts.json() if item["provider"] == "binance"] == ["Binance"]
    internal_accounts = client.get("/api/v1/accounts?include_internal=true")
    assert internal_accounts.status_code == 200
    assert len([item for item in internal_accounts.json() if item["provider"] == "binance"]) == 3

    raw_count = db_session.scalar(select(func.count()).select_from(RawEvent))
    ledger_count = db_session.scalar(select(func.count()).select_from(LedgerEvent))
    second = BinanceSyncService(db_session, settings, client_factory=FakeBinanceClient).run(connection.id, request)
    assert second.status == SyncRunStatus.SUCCEEDED
    assert second.stats_json["raw_created"] == 0
    assert second.stats_json["raw_existing"] == raw_count
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == raw_count
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == ledger_count


def test_pending_wallet_history_is_normalized_when_the_same_raw_event_completes(db_session):
    connection = seed_connection(db_session)
    settings = Settings(master_encryption_key=TEST_KEY)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    request = BinanceSyncRequest(
        products=["spot"],
        spot_symbols=["BTCUSDT"],
        history_start=end - timedelta(days=1),
        history_end=end,
    )
    PendingThenCompletedBinanceClient.completed = False
    first = BinanceSyncService(db_session, settings, client_factory=PendingThenCompletedBinanceClient).run(connection.id, request)
    assert first.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent).where(LedgerEvent.event_type.in_([LedgerEventType.DEPOSIT, LedgerEventType.WITHDRAW]))) == 0

    PendingThenCompletedBinanceClient.completed = True
    second = BinanceSyncService(db_session, settings, client_factory=PendingThenCompletedBinanceClient).run(connection.id, request)
    assert second.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent).where(LedgerEvent.event_type.in_([LedgerEventType.DEPOSIT, LedgerEventType.WITHDRAW]))) == 2


def test_sync_refuses_key_with_withdrawal_permission(db_session):
    connection = seed_connection(db_session)
    settings = Settings(master_encryption_key=TEST_KEY)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    run = BinanceSyncService(db_session, settings, client_factory=UnsafeBinanceClient).run(
        connection.id,
        BinanceSyncRequest(products=["spot"], history_start=end - timedelta(hours=1), history_end=end),
    )
    assert run.status == SyncRunStatus.FAILED
    assert run.error_code == "BINANCE_UNSAFE_PERMISSIONS"
    assert "withdrawal permission" in run.error_message
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 0


def test_sync_refuses_key_with_trading_permission(db_session):
    connection = seed_connection(db_session)
    settings = Settings(master_encryption_key=TEST_KEY)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    run = BinanceSyncService(db_session, settings, client_factory=TradingBinanceClient).run(
        connection.id,
        BinanceSyncRequest(products=["spot"], history_start=end - timedelta(hours=1), history_end=end),
    )
    assert run.status == SyncRunStatus.FAILED
    assert run.error_code == "BINANCE_UNSAFE_PERMISSIONS"
    assert "trading permission" in run.error_message


class ClosedPositionBinanceClient(FakeBinanceClient):
    def signed_get(self, product: str, path: str, params=None):
        if path == "/api/v3/account":
            type(self).calls.append((product, path, dict(params or {})))
            return {"balances": [{"asset": "BTC", "free": "0", "locked": "0"}, {"asset": "USDT", "free": "1000", "locked": "0"}]}
        return super().signed_get(product, path, params)


def test_spot_symbols_are_discovered_from_current_holdings_and_persist_after_closing(db_session):
    connection = seed_connection(db_session)
    settings = Settings(master_encryption_key=TEST_KEY)
    end = datetime(2023, 11, 15, tzinfo=timezone.utc)
    request = BinanceSyncRequest(products=["spot"], history_start=end - timedelta(days=1), history_end=end)

    first = BinanceSyncService(db_session, settings, client_factory=FakeBinanceClient).run(connection.id, request)
    assert first.status == SyncRunStatus.SUCCEEDED
    scope = db_session.scalar(
        select(ConnectionMarketScope).where(
            ConnectionMarketScope.connection_id == connection.id,
            ConnectionMarketScope.symbol == "BTCUSDT",
        )
    )
    assert scope is not None
    assert scope.discovery_source == "balance"
    assert scope.last_synced_at.replace(tzinfo=timezone.utc) == end
    assert first.stats_json["spot_symbols_discovered"] == 1
    assert first.stats_json["spot_symbols_synced"] == 1

    later = end + timedelta(hours=1)
    second = BinanceSyncService(db_session, settings, client_factory=ClosedPositionBinanceClient).run(
        connection.id,
        BinanceSyncRequest(products=["spot"], history_end=later),
    )
    assert second.status == SyncRunStatus.SUCCEEDED
    assert any(path == "/api/v3/myTrades" and params.get("symbol") == "BTCUSDT" for _, path, params in ClosedPositionBinanceClient.calls)
    assert second.stats_json["spot_symbols_discovered"] == 0
    assert second.stats_json["spot_symbols_synced"] == 1


def test_futures_trade_pagination_uses_trade_id_not_timestamp_boundary():
    client = SameMillisecondTradeClient()
    records = BinanceCollector(client).futures_trades(
        "usdm",
        datetime(2023, 11, 14, tzinfo=timezone.utc),
        datetime(2023, 11, 15, tzinfo=timezone.utc),
        usdm_symbols=["BTCUSDT"],
    )
    assert [record["id"] for record in records] == list(range(1, 1002))
    assert client.calls[1]["fromId"] == 1001
