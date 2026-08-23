import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.bitget.client import BitgetApiClient, BitgetApiError
from app.connectors.cex.storage import CexLedgerWriter, CexSyncStats, decimal_value, milliseconds_to_datetime
from app.core.config import Settings
from app.models import Account, AccountKind, ApiConnection, EntryDirection, LedgerEventType, SyncRun, SyncRunStatus, utc_now
from app.schemas import BitgetSyncRequest
from app.services.crypto import CredentialCipher


class BitgetPermissionError(ValueError):
    pass


class BitgetSyncService:
    READ_AUTHORITIES = {"coor", "cpor", "stor", "smor", "ttor", "wtor", "taxr", "chor", "p2pr", "pllr"}
    PRODUCT_CODES = {
        "usdt-futures": "USDT-FUTURES",
        "usdc-futures": "USDC-FUTURES",
        "coin-futures": "COIN-FUTURES",
    }

    def __init__(self, session: Session, settings: Settings, *, client_factory: type[BitgetApiClient] = BitgetApiClient) -> None:
        self.session = session
        self.settings = settings
        self.client_factory = client_factory
        self.stats = CexSyncStats()

    def run(self, connection_id: UUID, request: BitgetSyncRequest) -> SyncRun:
        connection = self.session.get(ApiConnection, connection_id)
        if not connection or not connection.is_enabled or connection.provider != "bitget":
            raise ValueError("connection not found, disabled, or not a Bitget connection")
        account = self.session.get(Account, connection.account_id)
        if not account:
            raise ValueError("connection account not found")
        if connection.requested_permissions != ["read"]:
            raise ValueError("connection is not read-only")
        end = request.history_end or utc_now()
        start = request.history_start or end - timedelta(days=90)
        if start >= end:
            raise ValueError("history_start must be before history_end")
        if end - start > timedelta(days=3650):
            raise ValueError("a single sync cannot exceed ten years")
        observed_at = datetime.combine(utc_now().date(), time.min, tzinfo=timezone.utc)
        run = SyncRun(connection_id=connection.id, requested_products=request.products)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        cipher = CredentialCipher(self.settings.master_encryption_key)
        api_key = cipher.decrypt(connection.encrypted_api_key)
        if not connection.encrypted_api_secret or not connection.encrypted_passphrase:
            self._finish(run.id, SyncRunStatus.FAILED, [], "BITGET_CREDENTIALS_REQUIRED", "Bitget API secret and passphrase are required")
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        secret = cipher.decrypt(connection.encrypted_api_secret)
        passphrase = cipher.decrypt(connection.encrypted_passphrase)
        writer = CexLedgerWriter(self.session, "bitget", self.stats)
        warnings: list[str] = []
        try:
            with self.client_factory(
                api_key=api_key,
                api_secret=secret,
                passphrase=passphrase,
                base_url=self.settings.bitget_base_url,
                timeout_seconds=self.settings.bitget_request_timeout_seconds,
                max_retries=self.settings.bitget_max_retries,
            ) as client:
                info = client.signed_get("/api/v2/spot/account/info") or {}
                writer.raw(account, connection, "account", f"permissions:{observed_at.date()}", "api_permissions", observed_at, info if isinstance(info, dict) else {})
                authorities = {str(value).lower() for value in (info.get("authorities", []) if isinstance(info, dict) else [])}
                unsafe = sorted(value for value in authorities if value not in self.READ_AUTHORITIES)
                if not authorities or unsafe:
                    raise BitgetPermissionError("Bitget API key has write, transfer, withdrawal, or unknown permissions; use a dedicated read-only key")
                self._sync_balances_and_positions(client, writer, connection, account, request, observed_at)
                self._sync_wallet_history(client, writer, connection, account, start, end)
                self._sync_spot_fills(client, writer, connection, account, request, start, end, warnings)
                self._sync_futures(client, writer, connection, account, request, start, end, warnings)
                self.session.commit()
        except BitgetPermissionError as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "BITGET_UNSAFE_PERMISSIONS", str(error))
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except BitgetApiError as error:
            self.session.rollback()
            code = f"BITGET_{error.code}" if error.code is not None else "BITGET_API_ERROR"
            self._finish(run.id, SyncRunStatus.FAILED, warnings, code, str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except Exception as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "BITGET_SYNC_ERROR", str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        self._finish(run.id, SyncRunStatus.PARTIAL if warnings else SyncRunStatus.SUCCEEDED, warnings)
        return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

    def _sync_balances_and_positions(self, client, writer, connection, account, request, observed_at) -> None:
        if "spot" in request.products:
            spot = client.signed_get("/api/v2/spot/account/assets") or []
            payload = {"assets": spot if isinstance(spot, list) else []}
            writer.raw(account, connection, "spot", f"balances:{observed_at.date()}", "balances", observed_at, payload)
            for item in payload["assets"]:
                if not isinstance(item, dict) or not item.get("coin"):
                    continue
                quantity = decimal_value(item.get("available")) + decimal_value(item.get("frozen")) + decimal_value(item.get("locked"))
                asset = writer.asset(str(item["coin"]), "spot")
                writer.balance(account.id, asset.id, quantity, "spot", observed_at)

        for product in request.products:
            product_code = self.PRODUCT_CODES.get(product)
            if not product_code:
                continue
            product_account = self._product_account(account, product)
            equity = Decimal("0")
            available = Decimal("0")
            unrealized = Decimal("0")
            margin_used = Decimal("0")
            accounts = client.signed_get("/api/v2/mix/account/accounts", {"productType": product_code}) or []
            account_rows = accounts if isinstance(accounts, list) else []
            raw, _ = writer.raw(product_account, connection, product, f"accounts:{observed_at.date()}", "accounts", observed_at, {"accounts": account_rows})
            for item in account_rows:
                if not isinstance(item, dict) or not item.get("marginCoin"):
                    continue
                coin = str(item["marginCoin"])
                quantity = decimal_value(item.get("equity") or item.get("accountEquity"))
                asset = writer.asset(coin, product)
                writer.balance(product_account.id, asset.id, quantity, product, observed_at)
                usd_value = decimal_value(item.get("usdtEquity"), quantity if coin in {"USDT", "USDC"} else Decimal("0"))
                equity += usd_value
                available += decimal_value(item.get("available"))
                unrealized += decimal_value(item.get("unrealizedPL"))
                margin_used += decimal_value(item.get("locked"))
            positions = client.signed_get("/api/v2/mix/position/all-position", {"productType": product_code}) or []
            position_rows = positions if isinstance(positions, list) else []
            position_raw, _ = writer.raw(product_account, connection, product, f"positions:{observed_at.date()}", "positions", observed_at, {"positions": position_rows})
            for item in position_rows:
                if not isinstance(item, dict):
                    continue
                quantity = decimal_value(item.get("total"))
                if quantity == 0:
                    continue
                side = str(item.get("holdSide") or "UNKNOWN")
                writer.position(
                    product_account,
                    position_raw,
                    product,
                    str(item.get("symbol", "")).upper(),
                    side,
                    quantity if side.lower() != "short" else -quantity,
                    observed_at,
                    entry_price=decimal_value(item.get("openPriceAvg")),
                    mark_price=decimal_value(item.get("markPrice")),
                    unrealized_pnl=decimal_value(item.get("unrealizedPL")),
                    leverage=decimal_value(item.get("leverage")),
                    liquidation_price=decimal_value(item.get("liquidationPrice")),
                    notional=abs(quantity) * decimal_value(item.get("markPrice")),
                    margin_asset=item.get("marginCoin"),
                    isolated=str(item.get("marginMode", "")).lower() == "isolated",
                    metadata={"realized_pnl": item.get("achievedProfits"), "funding_total": item.get("totalFee")},
                )
            writer.equity(product_account, raw, observed_at, equity=equity, withdrawable=available, margin_used=margin_used, unrealized_pnl=unrealized)

    @staticmethod
    def _windows(start: datetime, end: datetime, days: int):
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=days) - timedelta(milliseconds=1), end)
            yield int(cursor.timestamp() * 1000), int(window_end.timestamp() * 1000)
            cursor = window_end + timedelta(milliseconds=1)

    def _pages(self, client, path: str, params: dict[str, Any], *, list_key: str | None = None, cursor_key: str = "idLessThan") -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        for _ in range(200):
            data = client.signed_get(path, {**params, cursor_key: cursor, "limit": 100})
            container = data if isinstance(data, dict) else {}
            batch = container.get(list_key, []) if list_key else (data if isinstance(data, list) else [])
            batch = [item for item in batch if isinstance(item, dict)]
            yield from batch
            next_cursor = container.get("endId") if isinstance(container, dict) else None
            if not next_cursor and batch:
                next_cursor = batch[-1].get("tradeId") or batch[-1].get("orderId") or batch[-1].get("billId")
            if not next_cursor or next_cursor == cursor or len(batch) < 100:
                return
            cursor = str(next_cursor)
        raise ValueError(f"Bitget pagination safety limit exceeded for {path}")

    def _sync_wallet_history(self, client, writer, connection, account, start, end) -> None:
        for start_ms, end_ms in self._windows(start, end, 30):
            for kind, path in (("deposit", "/api/v2/spot/wallet/deposit-records"), ("withdraw", "/api/v2/spot/wallet/withdrawal-records")):
                for item in self._pages(client, path, {"startTime": start_ms, "endTime": end_ms}):
                    occurred = milliseconds_to_datetime(item.get("uTime") or item.get("cTime"), end)
                    external = str(item.get("orderId") or item.get("tradeId") or self._hash(item))
                    raw, _ = writer.raw(account, connection, "wallet", f"{kind}:{external}", kind, occurred, item)
                    if str(item.get("status", "")).lower() != "success":
                        continue
                    asset = writer.asset(str(item.get("coin", "")), "wallet")
                    amount = decimal_value(item.get("size"))
                    if kind == "deposit":
                        writer.ledger(raw, account, LedgerEventType.DEPOSIT, occurred, [(asset, EntryDirection.CREDIT, amount, False)], tx_hash=item.get("tradeId"), external_reference=item.get("orderId"), metadata={"chain": item.get("chain")})
                    else:
                        legs = [(asset, EntryDirection.DEBIT, amount, False)]
                        fee = abs(decimal_value(item.get("fee")))
                        if fee > 0:
                            legs.append((asset, EntryDirection.DEBIT, fee, True))
                        writer.ledger(raw, account, LedgerEventType.WITHDRAW, occurred, legs, tx_hash=item.get("tradeId"), external_reference=item.get("orderId"), metadata={"chain": item.get("chain")})

    def _sync_spot_fills(self, client, writer, connection, account, request, start, end, warnings) -> None:
        if "spot" not in request.products:
            return
        bounded = max(start, end - timedelta(days=90))
        if start < bounded:
            warnings.append("Bitget Spot fill API retains only the most recent 90 days; older history must be imported separately.")
        for start_ms, end_ms in self._windows(bounded, end, 30):
            for item in self._pages(client, "/api/v2/spot/trade/fills", {"startTime": start_ms, "endTime": end_ms}):
                symbol = str(item.get("symbol", "")).upper()
                if request.spot_symbols and symbol not in request.spot_symbols:
                    continue
                base_symbol, quote_symbol = self._split_symbol(symbol)
                if not base_symbol:
                    warnings.append(f"Bitget Spot symbol could not be normalized: {symbol}")
                    continue
                occurred = milliseconds_to_datetime(item.get("cTime"), end)
                external = str(item.get("tradeId") or self._hash(item))
                raw, created = writer.raw(account, connection, "spot", f"trade:{external}", "trade", occurred, item)
                if not created:
                    continue
                base, quote = writer.asset(base_symbol, "spot"), writer.asset(quote_symbol, "spot")
                is_buy = str(item.get("side", "")).lower() == "buy"
                quantity = decimal_value(item.get("size") or item.get("baseVolume"))
                quote_quantity = decimal_value(item.get("amount") or item.get("quoteVolume"), quantity * decimal_value(item.get("price")))
                legs = [(base, EntryDirection.CREDIT if is_buy else EntryDirection.DEBIT, quantity, False), (quote, EntryDirection.DEBIT if is_buy else EntryDirection.CREDIT, quote_quantity, False)]
                for fee in item.get("feeDetail", []) if isinstance(item.get("feeDetail"), list) else []:
                    fee_amount = abs(decimal_value(fee.get("totalFee") or fee.get("totalDeductionFee")))
                    if fee_amount > 0 and fee.get("feeCoin"):
                        legs.append((writer.asset(str(fee["feeCoin"]), "spot"), EntryDirection.DEBIT, fee_amount, True))
                writer.ledger(raw, account, LedgerEventType.BUY if is_buy else LedgerEventType.SELL, occurred, legs, external_reference=item.get("orderId"), metadata={"symbol": symbol, "price": item.get("price")})

    def _sync_futures(self, client, writer, connection, account, request, start, end, warnings) -> None:
        bounded = max(start, end - timedelta(days=90))
        if start < bounded and any(product in self.PRODUCT_CODES for product in request.products):
            warnings.append("Bitget futures fills and bills retain only the most recent 90 days; older history must be imported separately.")
        for product in request.products:
            product_code = self.PRODUCT_CODES.get(product)
            if not product_code:
                continue
            product_account = self._product_account(account, product)
            for start_ms, end_ms in self._windows(bounded, end, 30):
                for item in self._pages(client, "/api/v2/mix/order/fills", {"productType": product_code, "startTime": start_ms, "endTime": end_ms}, list_key="fillList"):
                    occurred = milliseconds_to_datetime(item.get("cTime"), end)
                    external = str(item.get("tradeId") or self._hash(item))
                    raw, created = writer.raw(product_account, connection, product, f"trade:{external}", "trade", occurred, item)
                    if not created:
                        continue
                    symbol = str(item.get("symbol", "")).upper()
                    base_symbol, _ = self._split_symbol(symbol)
                    if not base_symbol:
                        continue
                    base = writer.asset(base_symbol, product)
                    is_buy = str(item.get("side", "")).lower() == "buy"
                    legs = [(base, EntryDirection.CREDIT if is_buy else EntryDirection.DEBIT, decimal_value(item.get("baseVolume")), False)]
                    for fee in item.get("feeDetail", []) if isinstance(item.get("feeDetail"), list) else []:
                        amount = abs(decimal_value(fee.get("totalFee") or fee.get("totalDeductionFee")))
                        if amount > 0 and fee.get("feeCoin"):
                            legs.append((writer.asset(str(fee["feeCoin"]), product), EntryDirection.DEBIT, amount, True))
                    writer.ledger(raw, product_account, LedgerEventType.BUY if is_buy else LedgerEventType.SELL, occurred, legs, external_reference=item.get("orderId"), metadata={"market_type": product, "symbol": symbol, "price": item.get("price")})
                for item in self._pages(client, "/api/v2/mix/account/bill", {"productType": product_code, "startTime": start_ms, "endTime": end_ms}, list_key="bills"):
                    occurred = milliseconds_to_datetime(item.get("cTime"), end)
                    external = str(item.get("billId") or self._hash(item))
                    raw, _ = writer.raw(product_account, connection, product, f"bill:{external}", "account_bill", occurred, item)
                    amount = decimal_value(item.get("amount"))
                    fee = decimal_value(item.get("fee"))
                    business = str(item.get("businessType", ""))
                    coin = str(item.get("coin", ""))
                    if not coin or amount == 0 and fee == 0:
                        continue
                    asset = writer.asset(coin, product)
                    value = amount if amount != 0 else fee
                    event_type = LedgerEventType.FUNDING if business == "contract_settle_fee" else LedgerEventType.MANUAL_ADJUSTMENT
                    if business.startswith("force_close") or business.startswith("burst_"):
                        event_type = LedgerEventType.LIQUIDATION
                    writer.ledger(raw, product_account, event_type, occurred, [(asset, EntryDirection.CREDIT if value > 0 else EntryDirection.DEBIT, abs(value), fee != 0 and amount == 0)], external_reference=external, metadata={"business_type": business, "symbol": item.get("symbol")})

    def _product_account(self, root: Account, product: str) -> Account:
        external_id = f"{root.id}:{product}"
        account = self.session.scalar(
            select(Account).where(
                Account.portfolio_id == root.portfolio_id,
                Account.provider == "bitget",
                Account.external_account_id == external_id,
            )
        )
        if account:
            return account
        account = Account(
            portfolio_id=root.portfolio_id,
            kind=AccountKind.EXCHANGE,
            provider="bitget",
            label=f"{root.label} · {product.upper()}",
            external_account_id=external_id,
        )
        self.session.add(account)
        self.session.flush()
        return account

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str, str]:
        for quote in ("USDT", "USDC", "BTC", "ETH", "EUR", "USD"):
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
