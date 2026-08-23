import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.connectors.bybit.client import BybitApiClient, BybitApiError
from app.connectors.cex.storage import CexLedgerWriter, CexSyncStats, decimal_value, milliseconds_to_datetime
from app.core.config import Settings
from app.models import Account, ApiConnection, EntryDirection, LedgerEventType, SyncRun, SyncRunStatus, utc_now
from app.schemas import BybitSyncRequest
from app.services.crypto import CredentialCipher


class BybitPermissionError(ValueError):
    pass


class BybitSyncService:
    def __init__(self, session: Session, settings: Settings, *, client_factory: type[BybitApiClient] = BybitApiClient) -> None:
        self.session = session
        self.settings = settings
        self.client_factory = client_factory
        self.stats = CexSyncStats()

    @staticmethod
    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def run(self, connection_id: UUID, request: BybitSyncRequest) -> SyncRun:
        connection = self.session.get(ApiConnection, connection_id)
        if not connection or not connection.is_enabled or connection.provider != "bybit":
            raise ValueError("connection not found, disabled, or not a Bybit connection")
        account = self.session.get(Account, connection.account_id)
        if not account:
            raise ValueError("connection account not found")
        if connection.requested_permissions != ["read"]:
            raise ValueError("connection is not read-only")

        end = request.history_end or utc_now()
        start = request.history_start or end - timedelta(days=90)
        if start >= end:
            raise ValueError("history_start must be before history_end")
        if end - start > timedelta(days=730):
            raise ValueError("Bybit V5 history is limited to two years")
        observed_at = datetime.combine(utc_now().date(), time.min, tzinfo=timezone.utc)
        run = SyncRun(connection_id=connection.id, requested_products=request.products)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        cipher = CredentialCipher(self.settings.master_encryption_key)
        api_key = cipher.decrypt(connection.encrypted_api_key)
        if not connection.encrypted_api_secret:
            self._finish(run.id, SyncRunStatus.FAILED, [], "BYBIT_SECRET_REQUIRED", "Bybit API secret is required")
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        secret = cipher.decrypt(connection.encrypted_api_secret)
        writer = CexLedgerWriter(self.session, "bybit", self.stats)
        warnings: list[str] = []
        try:
            with self.client_factory(
                api_key=api_key,
                api_secret=secret,
                base_url=self.settings.bybit_base_url,
                timeout_seconds=self.settings.bybit_request_timeout_seconds,
                max_retries=self.settings.bybit_max_retries,
            ) as client:
                permission = self._result(client.signed_get("/v5/user/query-api"))
                writer.raw(account, connection, "account", f"permissions:{observed_at.date()}", "api_permissions", observed_at, permission)
                if int(permission.get("readOnly", 0)) != 1:
                    raise BybitPermissionError("Bybit API key is not read-only; revoke trading and withdrawal permissions")
                self._sync_balances(client, writer, connection, account, observed_at)
                self._sync_positions(client, writer, connection, account, request, observed_at, warnings)
                self._sync_wallet_history(client, writer, connection, account, start, end)
                self._sync_transactions(client, writer, connection, account, request, start, end, warnings)
                self.session.commit()
        except BybitPermissionError as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "BYBIT_UNSAFE_PERMISSIONS", str(error))
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except BybitApiError as error:
            self.session.rollback()
            code = f"BYBIT_{error.code}" if error.code is not None else "BYBIT_API_ERROR"
            self._finish(run.id, SyncRunStatus.FAILED, warnings, code, str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except Exception as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "BYBIT_SYNC_ERROR", str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

        self._finish(run.id, SyncRunStatus.PARTIAL if warnings else SyncRunStatus.SUCCEEDED, warnings)
        return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

    def _sync_balances(self, client: BybitApiClient, writer: CexLedgerWriter, connection, account, observed_at: datetime) -> None:
        payload = self._result(client.signed_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}))
        raw, _ = writer.raw(account, connection, "unified", f"wallet:{observed_at.date()}", "wallet_balance", observed_at, payload)
        rows = payload.get("list", [])
        if not isinstance(rows, list) or not rows:
            return
        wallet = rows[0] if isinstance(rows[0], dict) else {}
        for coin in wallet.get("coin", []):
            if not isinstance(coin, dict) or not coin.get("coin"):
                continue
            quantity = decimal_value(coin.get("walletBalance")) - decimal_value(coin.get("borrowAmount"))
            asset = writer.asset(str(coin["coin"]), "unified")
            writer.balance(account.id, asset.id, quantity, "unified", observed_at)
        writer.equity(
            account,
            raw,
            observed_at,
            equity=decimal_value(wallet.get("totalEquity")),
            withdrawable=decimal_value(wallet.get("totalAvailableBalance")),
            margin_used=decimal_value(wallet.get("totalInitialMargin")),
            unrealized_pnl=decimal_value(wallet.get("totalPerpUPL")),
            metadata={"account_type": wallet.get("accountType")},
        )

    def _sync_positions(self, client, writer, connection, account, request, observed_at, warnings) -> None:
        scopes: list[tuple[str, str]] = []
        if "linear" in request.products:
            scopes.extend(("linear", coin) for coin in request.linear_settle_coins)
        if "inverse" in request.products:
            scopes.extend(("inverse", coin) for coin in request.inverse_settle_coins)
        for category, settle_coin in scopes:
            payload = self._result(client.signed_get("/v5/position/list", {"category": category, "settleCoin": settle_coin, "limit": 200}))
            raw, _ = writer.raw(account, connection, category, f"positions:{settle_coin}:{observed_at.date()}", "positions", observed_at, payload)
            for item in payload.get("list", []):
                if not isinstance(item, dict):
                    continue
                quantity = decimal_value(item.get("size"))
                if quantity == 0:
                    continue
                side = str(item.get("side") or "UNKNOWN")
                writer.position(
                    account,
                    raw,
                    category,
                    str(item.get("symbol", "")),
                    side,
                    quantity if side.lower() != "sell" else -quantity,
                    observed_at,
                    entry_price=decimal_value(item.get("avgPrice")),
                    mark_price=decimal_value(item.get("markPrice")),
                    unrealized_pnl=decimal_value(item.get("unrealisedPnl")),
                    leverage=decimal_value(item.get("leverage")),
                    liquidation_price=decimal_value(item.get("liqPrice")),
                    notional=decimal_value(item.get("positionValue")),
                    margin_asset=item.get("settleCoin") or settle_coin,
                    isolated=str(item.get("tradeMode")) == "1",
                )
        if "inverse" in request.products:
            warnings.append("Bybit inverse positions use configured settlement coins; add further coins through a bounded backfill when needed.")

    def _paged(self, client, path: str, params: dict[str, Any], row_key: str) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        for _ in range(200):
            page = self._result(client.signed_get(path, {**params, "cursor": cursor, "limit": 50}))
            batch = page.get(row_key, [])
            yield from (item for item in batch if isinstance(item, dict))
            next_cursor = page.get("nextPageCursor")
            if not next_cursor or next_cursor == cursor or not batch:
                return
            cursor = str(next_cursor)
        raise ValueError(f"Bybit pagination safety limit exceeded for {path}")

    @staticmethod
    def _windows(start: datetime, end: datetime, days: int):
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=days) - timedelta(milliseconds=1), end)
            yield int(cursor.timestamp() * 1000), int(window_end.timestamp() * 1000)
            cursor = window_end + timedelta(milliseconds=1)

    def _sync_wallet_history(self, client, writer, connection, account, start, end) -> None:
        for start_ms, end_ms in self._windows(start, end, 30):
            for item in self._paged(client, "/v5/asset/deposit/query-record", {"startTime": start_ms, "endTime": end_ms}, "rows"):
                occurred = milliseconds_to_datetime(item.get("successAt") or item.get("blockUpdateTime") or item.get("createTime"), end)
                external = str(item.get("id") or item.get("txID") or self._hash(item))
                raw, _ = writer.raw(account, connection, "wallet", f"deposit:{external}", "deposit", occurred, item)
                if str(item.get("status", "")).lower() in {"3", "success", "completed"}:
                    asset = writer.asset(str(item.get("coin", "")), "wallet")
                    writer.ledger(raw, account, LedgerEventType.DEPOSIT, occurred, [(asset, EntryDirection.CREDIT, decimal_value(item.get("amount")), False)], tx_hash=item.get("txID"), external_reference=item.get("id"), metadata={"chain": item.get("chain")})
            for item in self._paged(client, "/v5/asset/withdraw/query-record", {"startTime": start_ms, "endTime": end_ms, "withdrawType": 2}, "rows"):
                occurred = milliseconds_to_datetime(item.get("updateTime") or item.get("createTime"), end)
                external = str(item.get("withdrawId") or item.get("txID") or self._hash(item))
                raw, _ = writer.raw(account, connection, "wallet", f"withdraw:{external}", "withdraw", occurred, item)
                if str(item.get("status", "")).lower() in {"success", "completed", "withdrawsuccess"}:
                    asset = writer.asset(str(item.get("coin", "")), "wallet")
                    legs = [(asset, EntryDirection.DEBIT, decimal_value(item.get("amount")), False)]
                    fee = decimal_value(item.get("withdrawFee")) + decimal_value(item.get("tax"))
                    if fee > 0:
                        legs.append((asset, EntryDirection.DEBIT, fee, True))
                    writer.ledger(raw, account, LedgerEventType.WITHDRAW, occurred, legs, tx_hash=item.get("txID"), external_reference=item.get("withdrawId"), metadata={"chain": item.get("chain")})

    def _sync_transactions(self, client, writer, connection, account, request, start, end, warnings) -> None:
        bounded_start = max(start, end - timedelta(days=730))
        for start_ms, end_ms in self._windows(bounded_start, end, 7):
            for item in self._paged(client, "/v5/account/transaction-log", {"accountType": "UNIFIED", "startTime": start_ms, "endTime": end_ms}, "list"):
                kind = str(item.get("type", "")).upper()
                if kind == "TRADE":
                    continue
                occurred = milliseconds_to_datetime(item.get("transactionTime"), end)
                external = str(item.get("id") or item.get("tradeId") or self._hash(item))
                raw, _ = writer.raw(account, connection, "unified", f"transaction:{external}", "transaction", occurred, item)
                coin = str(item.get("currency", ""))
                amount = decimal_value(item.get("funding")) if kind == "SETTLEMENT" else decimal_value(item.get("change"))
                if not coin or amount == 0:
                    continue
                asset = writer.asset(coin, "unified")
                event_type = LedgerEventType.FUNDING if kind == "SETTLEMENT" else LedgerEventType.MANUAL_ADJUSTMENT
                writer.ledger(raw, account, event_type, occurred, [(asset, EntryDirection.CREDIT if amount > 0 else EntryDirection.DEBIT, abs(amount), False)], external_reference=external, metadata={"bybit_type": kind, "symbol": item.get("symbol")})

        if "spot" not in request.products:
            return
        trade_start = max(start, end - timedelta(days=730))
        for start_ms, end_ms in self._windows(trade_start, end, 7):
            params: dict[str, Any] = {"category": "spot", "startTime": start_ms, "endTime": end_ms}
            for item in self._paged(client, "/v5/execution/list", params, "list"):
                symbol = str(item.get("symbol", "")).upper()
                if request.spot_symbols and symbol not in request.spot_symbols:
                    continue
                base_symbol, quote_symbol = self._split_symbol(symbol)
                if not base_symbol or not quote_symbol:
                    warnings.append(f"Bybit Spot symbol could not be normalized: {symbol}")
                    continue
                occurred = milliseconds_to_datetime(item.get("execTime"), end)
                external = str(item.get("execId") or self._hash(item))
                raw, created = writer.raw(account, connection, "spot", f"trade:{external}", "trade", occurred, item)
                if not created:
                    continue
                base = writer.asset(base_symbol, "spot")
                quote = writer.asset(quote_symbol, "spot")
                is_buy = str(item.get("side", "")).lower() == "buy"
                quantity = decimal_value(item.get("execQty"))
                quote_quantity = quantity * decimal_value(item.get("execPrice"))
                legs = [(base, EntryDirection.CREDIT if is_buy else EntryDirection.DEBIT, quantity, False), (quote, EntryDirection.DEBIT if is_buy else EntryDirection.CREDIT, quote_quantity, False)]
                fee = decimal_value(item.get("execFee"))
                if fee > 0:
                    fee_symbol = str(item.get("feeCurrency") or (base_symbol if is_buy else quote_symbol))
                    legs.append((writer.asset(fee_symbol, "spot"), EntryDirection.DEBIT, fee, True))
                writer.ledger(raw, account, LedgerEventType.BUY if is_buy else LedgerEventType.SELL, occurred, legs, external_reference=item.get("orderId"), metadata={"symbol": symbol, "price": item.get("execPrice")})

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str, str]:
        for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH", "EUR", "USD"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return symbol[: -len(quote)], quote
        return "", ""

    def _finish(self, run_id: UUID, status: SyncRunStatus, warnings: list[str], code: str | None = None, message: str | None = None) -> None:
        run = self.session.get(SyncRun, run_id)
        if not run:
            return
        run.status = status
        run.stats_json = self.stats.as_dict()
        run.warnings_json = list(dict.fromkeys(warnings))[:100]
        run.error_code = code
        run.error_message = message
        run.finished_at = utc_now()
        self.session.commit()

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
