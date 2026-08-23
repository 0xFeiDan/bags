import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.binance.client import BinanceApiClient, BinanceApiError, BinanceProduct
from app.connectors.binance.collector import BinanceCollector
from app.core.config import Settings
from app.models import (
    Account,
    AccountKind,
    ApiConnection,
    Asset,
    AssetAlias,
    AssetType,
    BalanceSnapshot,
    ConnectionMarketScope,
    EntryDirection,
    EventSource,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    PositionSnapshot,
    RawEvent,
    RawEventStatus,
    SyncCursor,
    SyncRun,
    SyncRunStatus,
    utc_now,
)
from app.schemas import BinanceSyncRequest
from app.services.crypto import CredentialCipher


def decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def milliseconds_to_datetime(value: Any, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return fallback


def parse_binance_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, (int, float)):
        return milliseconds_to_datetime(value, fallback)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace(" ", "T"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return fallback


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SyncStats:
    def __init__(self) -> None:
        self.raw_created = 0
        self.raw_existing = 0
        self.ledger_created = 0
        self.balances_created = 0
        self.positions_created = 0
        self.spot_symbols_discovered = 0
        self.spot_symbols_synced = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_created": self.raw_created,
            "raw_existing": self.raw_existing,
            "ledger_created": self.ledger_created,
            "balances_created": self.balances_created,
            "positions_created": self.positions_created,
            "spot_symbols_discovered": self.spot_symbols_discovered,
            "spot_symbols_synced": self.spot_symbols_synced,
        }


class BinancePermissionError(ValueError):
    pass


class BinanceSyncService:
    SPOT_QUOTE_ASSETS = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")

    def __init__(self, session: Session, settings: Settings, *, client_factory: type[BinanceApiClient] = BinanceApiClient) -> None:
        self.session = session
        self.settings = settings
        self.client_factory = client_factory
        self.stats = SyncStats()

    def run(self, connection_id: UUID, request: BinanceSyncRequest) -> SyncRun:
        connection = self.session.get(ApiConnection, connection_id)
        if not connection or not connection.is_enabled:
            raise ValueError("connection not found or disabled")
        if connection.provider != "binance":
            raise ValueError("connection is not a Binance connection")
        root_account = self.session.get(Account, connection.account_id)
        if not root_account:
            raise ValueError("connection account not found")
        if "read" not in connection.requested_permissions or len(connection.requested_permissions) != 1:
            raise ValueError("connection is not read-only")

        end = request.history_end or utc_now()
        # State endpoints have no historical-as-of parameter.  Keep their
        # observation time current and stable for repeated runs in one UTC day;
        # never stamp today's state with a caller supplied historical end time.
        observed_at = datetime.combine(utc_now().date(), time.min, tzinfo=timezone.utc)
        start = request.history_start or (end - timedelta(days=90))
        if start >= end:
            raise ValueError("history_start must be before history_end")
        if end - start > timedelta(days=3650):
            raise ValueError("a single sync cannot exceed ten years")

        run = SyncRun(connection_id=connection.id, requested_products=request.products)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        cipher = CredentialCipher(self.settings.master_encryption_key)
        api_key = cipher.decrypt(connection.encrypted_api_key)
        if not connection.encrypted_api_secret:
            self._finish_failed(run.id, "BINANCE_SECRET_REQUIRED", "Binance HMAC API secret is required")
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        api_secret = cipher.decrypt(connection.encrypted_api_secret)
        base_urls = {
            "spot": self.settings.binance_spot_base_url,
            "usdm": self.settings.binance_usdm_base_url,
            "coinm": self.settings.binance_coinm_base_url,
        }

        warnings: list[str] = []
        completed: list[str] = []
        try:
            with self.client_factory(
                api_key=api_key,
                api_secret=api_secret,
                base_urls=base_urls,
                timeout_seconds=self.settings.binance_request_timeout_seconds,
                max_retries=self.settings.binance_max_retries,
            ) as client:
                collector = BinanceCollector(client)
                permissions = collector.api_restrictions()
                self._raw(root_account, connection, "spot", f"api_permissions:{int(observed_at.timestamp() * 1000)}", "api_permissions", observed_at, permissions)
                if permissions.get("enableReading") is False:
                    raise BinancePermissionError("Binance API key does not have reading permission")
                if permissions.get("enableWithdrawals") or permissions.get("enableWithdraw"):
                    raise BinancePermissionError("Binance API key has withdrawal permission enabled; revoke it before syncing")
                if permissions.get("enableSpotAndMarginTrading"):
                    raise BinancePermissionError("Binance API key has Spot/Margin trading permission enabled; use a dedicated read-only key")
                for product in request.products:
                    try:
                        account = self._product_account(root_account, product)
                        product_start = self._incremental_start(account.id, f"binance:{product}", start, request.history_start is not None)
                        exchange_info = collector.exchange_info(product)
                        self._persist_public_snapshot(account, connection, product, "exchange_info", exchange_info, observed_at)
                        if product == "spot":
                            history_complete = self._sync_spot(collector, connection, account, exchange_info, request, product_start, end, warnings, observed_at)
                        else:
                            history_complete = self._sync_futures(collector, connection, account, product, exchange_info, request, product_start, end, warnings, observed_at)
                        if history_complete:
                            self._update_cursor(account.id, f"binance:{product}", end)
                        else:
                            warnings.append(f"Binance {product} history was not complete; its cursor was not advanced.")
                        self.session.commit()
                        completed.append(product)
                    except Exception:
                        self.session.rollback()
                        raise
        except BinancePermissionError as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "BINANCE_UNSAFE_PERMISSIONS", str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except BinanceApiError as error:
            self.session.rollback()
            status = SyncRunStatus.PARTIAL if completed else SyncRunStatus.FAILED
            code = f"BINANCE_{error.code}" if error.code is not None else "BINANCE_API_ERROR"
            message = str(error)[:300]
            if error.retry_after:
                message = f"{message}; retry after {error.retry_after}s"
            self._finish(run.id, status, warnings, code, message)
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except Exception as error:
            self.session.rollback()
            status = SyncRunStatus.PARTIAL if completed else SyncRunStatus.FAILED
            self._finish(run.id, status, warnings, "BINANCE_SYNC_ERROR", str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

        status = SyncRunStatus.PARTIAL if warnings else SyncRunStatus.SUCCEEDED
        self._finish(run.id, status, warnings)
        return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

    def _sync_spot(
        self,
        collector: BinanceCollector,
        connection: ApiConnection,
        account: Account,
        exchange_info: dict[str, Any],
        request: BinanceSyncRequest,
        start: datetime,
        end: datetime,
        warnings: list[str],
        observed_at: datetime,
    ) -> bool:
        symbol_map = {
            item.get("symbol"): (item.get("baseAsset"), item.get("quoteAsset"))
            for item in exchange_info.get("symbols", [])
            if isinstance(item, dict) and item.get("symbol")
        }
        account_payload = collector.account("spot")
        self._raw(account, connection, "spot", f"account:{int(observed_at.timestamp() * 1000)}", "account", observed_at, account_payload)
        seen_balance_assets: set[UUID] = set()
        held_assets: set[str] = set()
        for balance in account_payload.get("balances", []):
            if not isinstance(balance, dict):
                continue
            quantity = decimal_value(balance.get("free")) + decimal_value(balance.get("locked"))
            asset_symbol = str(balance.get("asset", "")).strip().upper()
            asset = self._asset(asset_symbol, "spot")
            seen_balance_assets.add(asset.id)
            self._balance(account.id, asset.id, quantity, "binance:spot", observed_at)
            if quantity > 0:
                held_assets.add(asset_symbol)
        self._zero_missing_balances(account.id, seen_balance_assets, "binance:spot", observed_at)

        for deposit in collector.wallet_history("deposit", start, end):
            occurred_at = milliseconds_to_datetime(deposit.get("insertTime"), end)
            external_id = str(deposit.get("id") or deposit.get("txId") or self._hash(deposit))
            raw, created = self._raw(account, connection, "spot", f"deposit:{external_id}", "deposit", occurred_at, deposit)
            if int(deposit.get("status", -1)) in {1, 6}:
                self._refresh_raw(raw, occurred_at, deposit)
                asset = self._asset(str(deposit.get("coin", "")), "spot")
                self._ledger(raw, account, LedgerEventType.DEPOSIT, occurred_at, [(asset, EntryDirection.CREDIT, decimal_value(deposit.get("amount")), False)], tx_hash=deposit.get("txId"), metadata={"network": deposit.get("network"), "transfer_type": deposit.get("transferType")})

        for withdrawal in collector.wallet_history("withdraw", start, end):
            occurred_at = parse_binance_datetime(withdrawal.get("completeTime") or withdrawal.get("applyTime"), end)
            external_id = str(withdrawal.get("id") or withdrawal.get("txId") or self._hash(withdrawal))
            raw, created = self._raw(account, connection, "spot", f"withdraw:{external_id}", "withdraw", occurred_at, withdrawal)
            if int(withdrawal.get("status", -1)) == 6:
                self._refresh_raw(raw, occurred_at, withdrawal)
                asset = self._asset(str(withdrawal.get("coin", "")), "spot")
                legs = [(asset, EntryDirection.DEBIT, decimal_value(withdrawal.get("amount")), False)]
                fee = decimal_value(withdrawal.get("transactionFee"))
                if fee > 0:
                    legs.append((asset, EntryDirection.DEBIT, fee, True))
                self._ledger(raw, account, LedgerEventType.WITHDRAW, occurred_at, legs, tx_hash=withdrawal.get("txId"), metadata={"network": withdrawal.get("network"), "address": withdrawal.get("address")})

        invalid = [symbol for symbol in request.spot_symbols if symbol not in symbol_map]
        if invalid:
            warnings.append(f"Unknown Spot symbols skipped: {', '.join(invalid[:10])}")
        valid_requested = [symbol for symbol in request.spot_symbols if symbol in symbol_map]
        scopes = self._spot_scopes(connection, symbol_map, held_assets, valid_requested)
        valid_scopes = [scope for scope in scopes if scope.symbol in symbol_map]
        unknown_saved = [scope.symbol for scope in scopes if scope.symbol not in symbol_map]
        if unknown_saved:
            warnings.append(f"Saved Spot symbols no longer listed by Binance and were skipped: {', '.join(unknown_saved[:10])}")
        if not valid_scopes:
            warnings.append("Spot balances and wallet history synced; no trade symbols were found from current non-zero holdings. Previously closed assets are intentionally not scanned.")
            return False
        for scope in valid_scopes:
            symbol_start = start
            if scope.last_synced_at and request.history_start is None:
                symbol_start = max(start, as_utc(scope.last_synced_at) - timedelta(minutes=5))
            for trade in collector.spot_trades([scope.symbol], symbol_start, end):
                symbol = str(trade.get("symbol", ""))
                base_symbol, quote_symbol = symbol_map[symbol]
                occurred_at = milliseconds_to_datetime(trade.get("time"), end)
                external_id = f"trade:{symbol}:{trade.get('id')}"
                raw, created = self._raw(account, connection, "spot", external_id, "trade", occurred_at, trade)
                if not created:
                    continue
                base = self._asset(str(base_symbol), "spot")
                quote = self._asset(str(quote_symbol), "spot")
                is_buyer = bool(trade.get("isBuyer"))
                legs = [
                    (base, EntryDirection.CREDIT if is_buyer else EntryDirection.DEBIT, decimal_value(trade.get("qty")), False),
                    (quote, EntryDirection.DEBIT if is_buyer else EntryDirection.CREDIT, decimal_value(trade.get("quoteQty")), False),
                ]
                commission = decimal_value(trade.get("commission"))
                if commission > 0 and trade.get("commissionAsset"):
                    legs.append((self._asset(str(trade["commissionAsset"]), "spot"), EntryDirection.DEBIT, commission, True))
                self._ledger(raw, account, LedgerEventType.BUY if is_buyer else LedgerEventType.SELL, occurred_at, legs, external_reference=str(trade.get("orderId")), metadata={"market_type": "spot", "symbol": symbol, "is_maker": trade.get("isMaker"), "price": trade.get("price")})
            scope.last_synced_at = end
            self.stats.spot_symbols_synced += 1
        return True

    def _spot_scopes(
        self,
        connection: ApiConnection,
        symbol_map: dict[str, tuple[str | None, str | None]],
        held_assets: set[str],
        requested_symbols: list[str],
    ) -> list[ConnectionMarketScope]:
        existing = {
            item.symbol: item
            for item in self.session.scalars(
                select(ConnectionMarketScope).where(
                    ConnectionMarketScope.connection_id == connection.id,
                    ConnectionMarketScope.product == "spot",
                )
            )
        }

        previously_seen = {
            str(payload.get("symbol", "")).strip().upper()
            for payload in self.session.scalars(
                select(RawEvent.payload_json).where(
                    RawEvent.connection_id == connection.id,
                    RawEvent.source == "binance:spot",
                    RawEvent.event_kind == "trade",
                )
            )
            if isinstance(payload, dict) and payload.get("symbol")
        }
        discovered = {
            symbol
            for symbol, (base_asset, quote_asset) in symbol_map.items()
            if str(base_asset or "").upper() in held_assets
            and str(quote_asset or "").upper() in self.SPOT_QUOTE_ASSETS
        }
        candidates = [
            *((symbol, "manual", True) for symbol in requested_symbols),
            *((symbol, "existing", False) for symbol in sorted(previously_seen)),
            *((symbol, "balance", False) for symbol in sorted(discovered)),
        ]
        for symbol, source, reactivate in candidates:
            scope = existing.get(symbol)
            if not scope:
                scope = ConnectionMarketScope(
                    connection_id=connection.id,
                    product="spot",
                    symbol=symbol,
                    discovery_source=source,
                )
                self.session.add(scope)
                existing[symbol] = scope
                self.stats.spot_symbols_discovered += 1
            elif reactivate:
                scope.is_active = True
                scope.discovery_source = "manual"
        self.session.flush()
        return sorted((scope for scope in existing.values() if scope.is_active), key=lambda item: item.symbol)

    def _sync_futures(
        self,
        collector: BinanceCollector,
        connection: ApiConnection,
        account: Account,
        product: BinanceProduct,
        exchange_info: dict[str, Any],
        request: BinanceSyncRequest,
        start: datetime,
        end: datetime,
        warnings: list[str],
        observed_at: datetime,
    ) -> bool:
        account_payload = collector.account(product)
        self._raw(account, connection, product, f"account:{int(observed_at.timestamp() * 1000)}", "account", observed_at, account_payload)
        seen_balance_assets: set[UUID] = set()
        for balance in account_payload.get("assets", []):
            if not isinstance(balance, dict):
                continue
            quantity = decimal_value(balance.get("walletBalance"))
            asset = self._asset(str(balance.get("asset", "")), product)
            seen_balance_assets.add(asset.id)
            self._balance(account.id, asset.id, quantity, f"binance:{product}", observed_at)
        self._zero_missing_balances(account.id, seen_balance_assets, f"binance:{product}", observed_at)

        contract_map = {
            str(item.get("symbol")): item
            for item in exchange_info.get("symbols", [])
            if isinstance(item, dict) and item.get("symbol")
        }
        positions = collector.positions(product)
        raw_positions, _ = self._raw(account, connection, product, f"positions:{int(observed_at.timestamp() * 1000)}", "positions", observed_at, {"positions": positions})
        active_coinm_pairs: set[str] = set()
        active_usdm_symbols: set[str] = set()
        for position in positions:
            quantity = decimal_value(position.get("positionAmt"))
            if quantity == 0:
                continue
            symbol = str(position.get("symbol", ""))
            contract = contract_map.get(symbol, {})
            if product == "coinm":
                pair = str(position.get("pair") or contract.get("pair") or symbol.split("_")[0]).upper()
                if pair:
                    active_coinm_pairs.add(pair)
            if product == "usdm" and position.get("symbol"):
                active_usdm_symbols.add(str(position["symbol"]).upper())
            existing_position = self.session.scalar(
                select(PositionSnapshot.id).where(
                    PositionSnapshot.account_id == account.id,
                    PositionSnapshot.product == product,
                    PositionSnapshot.symbol == symbol,
                    PositionSnapshot.position_side == str(position.get("positionSide") or ("LONG" if quantity > 0 else "SHORT")),
                    PositionSnapshot.as_of == observed_at,
                )
            )
            if existing_position:
                continue
            isolated_value = position.get("isolated")
            if isolated_value is None:
                isolated_value = decimal_value(position.get("isolatedMargin")) != 0
            snapshot = PositionSnapshot(
                account_id=account.id,
                source_raw_event_id=raw_positions.id,
                product=product,
                symbol=symbol,
                position_side=str(position.get("positionSide") or ("LONG" if quantity > 0 else "SHORT")),
                quantity=quantity,
                entry_price=decimal_value(position.get("entryPrice")),
                mark_price=decimal_value(position.get("markPrice")),
                unrealized_pnl=decimal_value(position.get("unRealizedProfit", position.get("unrealizedProfit"))),
                leverage=decimal_value(position.get("leverage")),
                liquidation_price=decimal_value(position.get("liquidationPrice")),
                notional=decimal_value(position.get("notionalValue", position.get("notional"))),
                margin_asset=position.get("marginAsset") or contract.get("marginAsset"),
                isolated=bool(isolated_value),
                as_of=observed_at,
                metadata_json={
                    "source_quantity_unit": "contracts" if product == "coinm" else "base_asset",
                    "contract_size": contract.get("contractSize"),
                    "base_asset": contract.get("baseAsset"),
                    "quote_asset": contract.get("quoteAsset"),
                },
            )
            self.session.add(snapshot)
            self.stats.positions_created += 1

        coinm_pairs = request.coinm_pairs or sorted(active_coinm_pairs)
        usdm_symbols = request.usdm_symbols or sorted(active_usdm_symbols)
        if product == "usdm" and not request.usdm_symbols:
            warnings.append("USD-M trades were limited to symbols with current positions; provide usdm_symbols to include fully closed markets.")
        if product == "coinm" and not coinm_pairs:
            warnings.append("COIN-M balances, positions and income synced; historical trades require coinm_pairs when there are no active positions.")
        trade_start = start
        income_start = start
        if product == "usdm":
            if start < end - timedelta(days=180):
                warnings.append("USD-M trade history is limited by Binance to the most recent six months.")
                trade_start = end - timedelta(days=180)
            if start < end - timedelta(days=90):
                warnings.append("USD-M income history is limited by Binance to the most recent three months.")
                income_start = end - timedelta(days=90)
        trades = collector.futures_trades(product, trade_start, end, usdm_symbols=usdm_symbols, coinm_pairs=coinm_pairs)
        for trade in trades:
            occurred_at = milliseconds_to_datetime(trade.get("time"), end)
            symbol = str(trade.get("symbol", ""))
            raw, created = self._raw(account, connection, product, f"trade:{symbol}:{trade.get('id')}", "trade", occurred_at, trade)
            if not created:
                continue
            commission = decimal_value(trade.get("commission"))
            if commission <= 0 or not trade.get("commissionAsset"):
                continue
            fee_asset = self._asset(str(trade["commissionAsset"]), product)
            event_type = LedgerEventType.BUY if str(trade.get("side", "")).upper() == "BUY" else LedgerEventType.SELL
            self._ledger(raw, account, event_type, occurred_at, [(fee_asset, EntryDirection.DEBIT, commission, True)], external_reference=str(trade.get("orderId")), metadata={"market_type": product, "derivative_trade": True, "symbol": symbol, "position_side": trade.get("positionSide"), "qty": trade.get("qty"), "price": trade.get("price"), "realized_pnl_source": "income_history"})

        for income in collector.futures_income(product, income_start, end):
            occurred_at = milliseconds_to_datetime(income.get("time"), end)
            income_type = str(income.get("incomeType", "UNKNOWN"))
            external_id = f"income:{income_type}:{income.get('tranId')}"
            raw, created = self._raw(account, connection, product, external_id, "income", occurred_at, income)
            if not created:
                continue
            if income_type == "COMMISSION":
                raw.status = RawEventStatus.IGNORED  # The matching trade already owns the fee ledger entry.
                continue
            amount = decimal_value(income.get("income"))
            if amount == 0 or not income.get("asset"):
                continue
            asset = self._asset(str(income["asset"]), product)
            event_type = LedgerEventType.INTERNAL_TRANSFER if income_type == "TRANSFER" else self._income_event_type(income_type, amount)
            self._ledger(
                raw,
                account,
                event_type,
                occurred_at,
                [(asset, EntryDirection.CREDIT if amount > 0 else EntryDirection.DEBIT, abs(amount), income_type == "COMMISSION")],
                external_reference=str(income.get("tradeId") or income.get("tranId")),
                metadata={"market_type": product, "income_type": income_type, "symbol": income.get("symbol"), "internal_account_transfer": income_type == "TRANSFER"},
            )
        return bool(request.usdm_symbols) if product == "usdm" else bool(request.coinm_pairs)

    def _product_account(self, root: Account, product: BinanceProduct) -> Account:
        if product == "spot":
            return root
        external_id = f"{root.id}:{product}"
        account = self.session.scalar(select(Account).where(Account.portfolio_id == root.portfolio_id, Account.provider == "binance", Account.external_account_id == external_id))
        if account:
            return account
        account = Account(portfolio_id=root.portfolio_id, kind=AccountKind.EXCHANGE, provider="binance", label=f"{root.label} · {product.upper()}", external_account_id=external_id)
        self.session.add(account)
        self.session.flush()
        return account

    def _asset(self, symbol: str, source: str) -> Asset:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("Binance payload contains an empty asset symbol")
        asset = self.session.scalar(select(Asset).where(Asset.canonical_symbol == symbol, Asset.chain_id.is_(None), Asset.contract_address.is_(None)))
        if not asset:
            stablecoins = {"USDT", "USDC", "FDUSD", "DAI", "USDE", "USD1", "PYUSD"}
            asset = Asset(canonical_symbol=symbol, name=symbol, asset_type=AssetType.STABLECOIN if symbol in stablecoins else AssetType.TOKEN, decimals=18)
            self.session.add(asset)
            self.session.flush()
        alias_source = f"binance:{source}"
        alias = self.session.scalar(select(AssetAlias).where(AssetAlias.source == alias_source, AssetAlias.source_asset_id == symbol))
        if not alias:
            self.session.add(AssetAlias(asset_id=asset.id, source=alias_source, source_asset_id=symbol, symbol=symbol))
            self.session.flush()
        return asset

    def _raw(
        self,
        account: Account,
        connection: ApiConnection,
        product: str,
        external_id: str,
        kind: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> tuple[RawEvent, bool]:
        source = f"binance:{product}"
        existing = self.session.scalar(select(RawEvent).where(RawEvent.account_id == account.id, RawEvent.source == source, RawEvent.external_event_id == external_id))
        if existing:
            self.stats.raw_existing += 1
            return existing, False
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        raw = RawEvent(
            account_id=account.id,
            connection_id=connection.id,
            source=source,
            external_event_id=external_id,
            event_kind=kind,
            occurred_at=occurred_at,
            payload_json=payload,
            payload_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            status=RawEventStatus.RECEIVED,
        )
        self.session.add(raw)
        self.session.flush()
        self.stats.raw_created += 1
        return raw, True

    def _refresh_raw(self, raw: RawEvent, occurred_at: datetime, payload: dict[str, Any]) -> None:
        if self.session.scalar(select(LedgerEvent.id).where(LedgerEvent.raw_event_id == raw.id)):
            return
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        raw.occurred_at = occurred_at
        raw.payload_json = payload
        raw.payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        raw.status = RawEventStatus.RECEIVED

    def _zero_missing_balances(self, account_id: UUID, seen_asset_ids: set[UUID], source: str, as_of: datetime) -> None:
        prior_assets = set(self.session.scalars(select(BalanceSnapshot.asset_id).where(BalanceSnapshot.account_id == account_id)))
        for asset_id in prior_assets - seen_asset_ids:
            self._balance(account_id, asset_id, Decimal("0"), source, as_of)

    def _persist_public_snapshot(self, account: Account, connection: ApiConnection, product: str, kind: str, payload: dict[str, Any], as_of: datetime) -> None:
        day = as_of.astimezone(timezone.utc).strftime("%Y-%m-%d")
        self._raw(account, connection, product, f"{kind}:{day}", kind, as_of, payload)

    def _ledger(
        self,
        raw: RawEvent,
        account: Account,
        event_type: LedgerEventType,
        occurred_at: datetime,
        legs: list[tuple[Asset, EntryDirection, Decimal, bool]],
        *,
        tx_hash: Any = None,
        external_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        valid_legs = [leg for leg in legs if leg[2] > 0]
        if not valid_legs or self.session.scalar(select(LedgerEvent.id).where(LedgerEvent.raw_event_id == raw.id)):
            return
        event = LedgerEvent(
            portfolio_id=account.portfolio_id,
            raw_event_id=raw.id,
            event_type=event_type,
            source=EventSource.RAW,
            status=EventStatus.POSTED,
            occurred_at=occurred_at,
            tx_hash=str(tx_hash) if tx_hash else None,
            external_reference=external_reference,
            metadata_json=metadata or {},
        )
        self.session.add(event)
        self.session.flush()
        for asset, direction, quantity, fee_flag in valid_legs:
            self.session.add(LedgerEntry(ledger_event_id=event.id, account_id=account.id, asset_id=asset.id, direction=direction, quantity=quantity, fee_flag=fee_flag))
        raw.status = RawEventStatus.NORMALIZED
        self.stats.ledger_created += 1

    def _update_cursor(self, account_id: UUID, resource: str, end: datetime) -> None:
        cursor = self.session.scalar(select(SyncCursor).where(SyncCursor.account_id == account_id, SyncCursor.resource == resource))
        if not cursor:
            cursor = SyncCursor(account_id=account_id, resource=resource)
            self.session.add(cursor)
        cursor.cursor_value = str(int(end.timestamp() * 1000))
        cursor.last_synced_at = end

    def _incremental_start(self, account_id: UUID, resource: str, fallback: datetime, explicit_start: bool) -> datetime:
        if explicit_start:
            return fallback
        cursor = self.session.scalar(select(SyncCursor).where(SyncCursor.account_id == account_id, SyncCursor.resource == resource))
        if not cursor or not cursor.last_synced_at:
            return fallback
        # A small overlap protects against eventually-consistent history endpoints; raw-event keys deduplicate it.
        return max(fallback, as_utc(cursor.last_synced_at) - timedelta(minutes=5))

    def _balance(self, account_id: UUID, asset_id: UUID, quantity: Decimal, source: str, as_of: datetime) -> None:
        exists = self.session.scalar(
            select(BalanceSnapshot.id).where(
                BalanceSnapshot.account_id == account_id,
                BalanceSnapshot.asset_id == asset_id,
                BalanceSnapshot.as_of == as_of,
            )
        )
        if exists:
            return
        self.session.add(BalanceSnapshot(account_id=account_id, asset_id=asset_id, quantity=quantity, source=source, as_of=as_of))
        self.stats.balances_created += 1

    @staticmethod
    def _income_event_type(income_type: str, amount: Decimal) -> LedgerEventType:
        if income_type == "FUNDING_FEE":
            return LedgerEventType.FUNDING
        if income_type in {"INSURANCE_CLEAR", "DELIVERED_SETTELMENT"}:
            return LedgerEventType.LIQUIDATION
        if income_type == "TRANSFER":
            return LedgerEventType.DEPOSIT if amount > 0 else LedgerEventType.WITHDRAW
        if income_type == "REALIZED_PNL":
            return LedgerEventType.SELL
        if income_type in {"WELCOME_BONUS", "REFERRAL_KICKBACK", "COMMISSION_REBATE"}:
            return LedgerEventType.INTEREST
        return LedgerEventType.MANUAL_ADJUSTMENT

    def _finish_failed(self, run_id: UUID, code: str, message: str) -> None:
        self._finish(run_id, SyncRunStatus.FAILED, [], code, message)

    def _finish(self, run_id: UUID, status: SyncRunStatus, warnings: list[str], error_code: str | None = None, error_message: str | None = None) -> None:
        run = self.session.get(SyncRun, run_id)
        if not run:
            return
        run.status = status
        run.stats_json = self.stats.as_dict()
        run.warnings_json = warnings
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = utc_now()
        self.session.commit()

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
