import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.perp_dex.hyperliquid.client import HyperliquidApiError, HyperliquidClient
from app.connectors.perp_dex.hyperliquid.collector import HyperliquidCollector
from app.core.config import Settings
from app.models import (
    Account,
    AccountEquitySnapshot,
    AccountKind,
    ApiConnection,
    Asset,
    AssetAlias,
    AssetPrice,
    AssetType,
    BalanceSnapshot,
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
from app.schemas import HyperliquidSyncRequest
from app.services.crypto import CredentialCipher

ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")


def decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default


def milliseconds_to_datetime(value: Any, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return fallback


class SyncStats:
    def __init__(self) -> None:
        self.raw_created = 0
        self.raw_existing = 0
        self.ledger_created = 0
        self.balances_created = 0
        self.positions_created = 0
        self.equity_created = 0
        self.prices_created = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_created": self.raw_created,
            "raw_existing": self.raw_existing,
            "ledger_created": self.ledger_created,
            "balances_created": self.balances_created,
            "positions_created": self.positions_created,
            "equity_created": self.equity_created,
            "prices_created": self.prices_created,
        }


class HyperliquidSyncService:
    def __init__(self, session: Session, settings: Settings, *, client_factory: type[HyperliquidClient] = HyperliquidClient) -> None:
        self.session = session
        self.settings = settings
        self.client_factory = client_factory
        self.stats = SyncStats()

    def run(self, connection_id: UUID, request: HyperliquidSyncRequest) -> SyncRun:
        connection = self.session.get(ApiConnection, connection_id)
        if not connection or not connection.is_enabled:
            raise ValueError("connection not found or disabled")
        if connection.provider != "hyperliquid":
            raise ValueError("connection is not a Hyperliquid connection")
        if connection.requested_permissions != ["read"]:
            raise ValueError("connection is not read-only")
        account = self.session.get(Account, connection.account_id)
        if not account:
            raise ValueError("connection account not found")
        if account.kind != AccountKind.PERP_DEX:
            raise ValueError("Hyperliquid connection requires a perp_dex account")

        address = self._resolve_address(account, connection)
        end = request.history_end or utc_now()
        # Hyperliquid state is current-only. Record each sync at its actual
        # observation time so a manual refresh cannot reuse an earlier daily
        # balance while history_end remains reserved for history pagination.
        observed_at = utc_now()
        start = request.history_start or (end - timedelta(days=90))
        if start >= end:
            raise ValueError("history_start must be before history_end")
        if end - start > timedelta(days=3650):
            raise ValueError("a single sync cannot exceed ten years")
        history_start = self._incremental_start(account.id, start, request.history_start is not None)
        products = ["hyperliquid_perp", "history"]
        if request.include_spot:
            products.append("hyperliquid_spot")

        run = SyncRun(connection_id=connection.id, requested_products=products)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        warnings: list[str] = []
        try:
            with self.client_factory(
                base_url=self.settings.hyperliquid_base_url,
                timeout_seconds=self.settings.hyperliquid_request_timeout_seconds,
                max_retries=self.settings.hyperliquid_max_retries,
            ) as client:
                collector = HyperliquidCollector(client)
                self._sync_state(collector, connection, account, address, observed_at, request.include_spot)
                history_complete = self._sync_history(collector, connection, account, address, history_start, end, warnings)
                if history_complete:
                    self._update_cursor(account.id, end)
                else:
                    warnings.append("Hyperliquid history was incomplete; its cursor was not advanced.")
                self.session.commit()
        except HyperliquidApiError as error:
            self.session.rollback()
            message = str(error)[:300]
            if error.retry_after:
                message = f"{message}; retry after {error.retry_after}s"
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "HYPERLIQUID_API_ERROR", message)
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]
        except Exception as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, warnings, "HYPERLIQUID_SYNC_ERROR", str(error)[:300])
            return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

        status = SyncRunStatus.PARTIAL if warnings else SyncRunStatus.SUCCEEDED
        self._finish(run.id, status, warnings)
        return self.session.get(SyncRun, run.id)  # type: ignore[return-value]

    def _resolve_address(self, account: Account, connection: ApiConnection) -> str:
        candidate = account.address or account.external_account_id
        if not candidate:
            candidate = CredentialCipher(self.settings.master_encryption_key).decrypt(connection.encrypted_api_key)
        candidate = candidate.strip().lower()
        if not ADDRESS_PATTERN.fullmatch(candidate):
            raise ValueError("Hyperliquid account requires a valid 42-character EVM address")
        return candidate

    def _sync_state(
        self,
        collector: HyperliquidCollector,
        connection: ApiConnection,
        account: Account,
        address: str,
        as_of: datetime,
        include_spot: bool,
    ) -> None:
        state = collector.clearinghouse_state(address)
        state_raw, _ = self._raw(account, connection, f"clearinghouse:{int(as_of.timestamp() * 1000)}", "clearinghouse_state", as_of, state)
        market_context = collector.perp_market_context()
        self._public_raw(account, connection, "perp_market_context", {"data": market_context}, as_of)
        marks = self._mark_prices(market_context)
        positions = state.get("assetPositions", [])
        unrealized_total = Decimal("0")
        for wrapper in positions:
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("position"), dict):
                continue
            position = wrapper["position"]
            quantity = decimal_value(position.get("szi"))
            if quantity == 0:
                continue
            unrealized = decimal_value(position.get("unrealizedPnl"))
            unrealized_total += unrealized
            coin = str(position.get("coin", ""))[:64]
            mark = marks.get(coin)
            if mark is None and quantity != 0:
                mark = decimal_value(position.get("positionValue")) / abs(quantity)
            leverage_payload = position.get("leverage") if isinstance(position.get("leverage"), dict) else {}
            side = "LONG" if quantity > 0 else "SHORT"
            if self.session.scalar(
                select(PositionSnapshot.id).where(
                    PositionSnapshot.account_id == account.id,
                    PositionSnapshot.product == "hyperliquid_perp",
                    PositionSnapshot.symbol == coin,
                    PositionSnapshot.position_side == side,
                    PositionSnapshot.as_of == as_of,
                )
            ):
                continue
            self.session.add(
                PositionSnapshot(
                    account_id=account.id,
                    source_raw_event_id=state_raw.id,
                    product="hyperliquid_perp",
                    symbol=coin,
                    position_side=side,
                    quantity=quantity,
                    entry_price=decimal_value(position.get("entryPx")),
                    mark_price=mark,
                    unrealized_pnl=unrealized,
                    leverage=decimal_value(leverage_payload.get("value")),
                    liquidation_price=decimal_value(position.get("liquidationPx")),
                    notional=decimal_value(position.get("positionValue")),
                    margin_asset="USDC",
                    isolated=str(leverage_payload.get("type", "cross")).lower() == "isolated",
                    as_of=as_of,
                    metadata_json={
                        "margin_used": position.get("marginUsed"),
                        "return_on_equity": position.get("returnOnEquity"),
                        "leverage_type": leverage_payload.get("type"),
                        "source_quantity_unit": "base_asset",
                    },
                )
            )
            self.stats.positions_created += 1

        summary = state.get("marginSummary") if isinstance(state.get("marginSummary"), dict) else {}
        if not self.session.scalar(
            select(AccountEquitySnapshot.id).where(
                AccountEquitySnapshot.account_id == account.id,
                AccountEquitySnapshot.provider == "hyperliquid",
                AccountEquitySnapshot.as_of == as_of,
            )
        ):
            self.session.add(
                AccountEquitySnapshot(
                    account_id=account.id,
                    source_raw_event_id=state_raw.id,
                    provider="hyperliquid",
                    # Hyperliquid's marginSummary.accountValue is the
                    # clearinghouse USD account-value result, not a raw token
                    # balance. Store that reporting unit explicitly so the
                    # dashboard does not require an unrelated USDC price row
                    # before it can display authoritative perp equity.
                    currency="USD",
                    equity=decimal_value(summary.get("accountValue")),
                    withdrawable=decimal_value(state.get("withdrawable")),
                    margin_used=decimal_value(summary.get("totalMarginUsed")),
                    total_notional=decimal_value(summary.get("totalNtlPos")),
                    unrealized_pnl=unrealized_total,
                    as_of=as_of,
                    metadata_json={
                        "total_raw_usd": summary.get("totalRawUsd"),
                        "address": address,
                        "equity_source_field": "marginSummary.accountValue",
                    },
                )
            )
            self.stats.equity_created += 1

        if not include_spot:
            return
        spot_state = collector.spot_state(address)
        self._raw(account, connection, f"spot_state:{int(as_of.timestamp() * 1000)}", "spot_state", as_of, spot_state)
        spot_market_context = collector.spot_market_context()
        spot_meta = spot_market_context[0] if len(spot_market_context) == 2 and isinstance(spot_market_context[0], dict) else {}
        self._public_raw(account, connection, "spot_market_context", {"data": spot_market_context}, as_of)
        token_names = {
            int(item["index"]): str(item["name"])
            for item in spot_meta.get("tokens", [])
            if isinstance(item, dict) and item.get("index") is not None and item.get("name")
        }
        spot_prices = self._spot_prices(spot_market_context)
        seen_balance_assets: set[UUID] = set()
        for balance in spot_state.get("balances", []):
            if not isinstance(balance, dict):
                continue
            quantity = decimal_value(balance.get("total"))
            token_index = balance.get("token")
            symbol = token_names.get(int(token_index), str(balance.get("coin", ""))) if token_index is not None else str(balance.get("coin", ""))
            asset = self._asset(symbol, "spot")
            seen_balance_assets.add(asset.id)
            self._balance(account.id, asset.id, quantity, as_of)
            price = spot_prices.get(int(token_index)) if token_index is not None else None
            if price is not None and symbol.upper() not in {"USD", "USDC", "USDT"}:
                self._price(asset.id, price, as_of, token_index=int(token_index))
        self._zero_missing_spot_balances(account.id, seen_balance_assets, as_of)

    def _sync_history(
        self,
        collector: HyperliquidCollector,
        connection: ApiConnection,
        account: Account,
        address: str,
        start: datetime,
        end: datetime,
        warnings: list[str],
    ) -> bool:
        complete = True
        fills = collector.fills(address, start, end)
        if fills.truncated:
            warnings.append("Hyperliquid only exposes the 10,000 most recent fills; older fills may require an exported statement.")
            complete = False
        for fill in fills.records:
            occurred_at = milliseconds_to_datetime(fill.get("time"), end)
            identity = f"fill:{fill.get('hash')}:{fill.get('tid', fill.get('oid'))}:{fill.get('time')}"
            raw, created = self._raw(account, connection, identity, "fill", occurred_at, fill)
            if not created:
                continue
            legs: list[tuple[Asset, EntryDirection, Decimal, bool]] = []
            if self._is_spot_fill(fill):
                base_symbol, quote_symbol = self._spot_fill_pair(fill)
                size = abs(decimal_value(fill.get("sz")))
                quote_quantity = size * decimal_value(fill.get("px"))
                is_buy = str(fill.get("dir", "")).strip().lower() == "buy" or str(fill.get("side", "")).upper() == "B"
                if size > 0 and quote_quantity > 0:
                    base = self._asset(base_symbol, "spot")
                    quote = self._asset(quote_symbol, "spot")
                    legs.extend(
                        [
                            (base, EntryDirection.CREDIT if is_buy else EntryDirection.DEBIT, size, False),
                            (quote, EntryDirection.DEBIT if is_buy else EntryDirection.CREDIT, quote_quantity, False),
                        ]
                    )
                fee = decimal_value(fill.get("fee"))
                if fee > 0:
                    legs.append((self._asset(str(fill.get("feeToken") or quote_symbol), "spot"), EntryDirection.DEBIT, fee, True))
                event_type = LedgerEventType.BUY if is_buy else LedgerEventType.SELL
                if legs:
                    self._ledger(raw, account, event_type, occurred_at, legs, tx_hash=fill.get("hash"), external_reference=str(fill.get("oid")), metadata={"market_type": "hyperliquid_spot", "symbol": fill.get("coin"), "direction": fill.get("dir"), "size": fill.get("sz"), "price": fill.get("px"), "trade_id": fill.get("tid")})
                else:
                    raw.status = RawEventStatus.IGNORED
                continue
            closed_pnl = decimal_value(fill.get("closedPnl"))
            if closed_pnl != 0:
                usdc = self._asset("USDC", "perp")
                legs.append((usdc, EntryDirection.CREDIT if closed_pnl > 0 else EntryDirection.DEBIT, abs(closed_pnl), False))
            fee = decimal_value(fill.get("fee"))
            if fee > 0:
                legs.append((self._asset(str(fill.get("feeToken") or "USDC"), "perp"), EntryDirection.DEBIT, fee, True))
            event_type = LedgerEventType.BUY if str(fill.get("side", "")).upper() == "B" else LedgerEventType.SELL
            if legs:
                self._ledger(raw, account, event_type, occurred_at, legs, tx_hash=fill.get("hash"), external_reference=str(fill.get("oid")), metadata={"market_type": "hyperliquid_perp", "symbol": fill.get("coin"), "direction": fill.get("dir"), "size": fill.get("sz"), "price": fill.get("px"), "closed_pnl": fill.get("closedPnl"), "trade_id": fill.get("tid")})
            else:
                raw.status = RawEventStatus.IGNORED

        funding = collector.funding(address, start, end)
        if funding.truncated:
            warnings.append("Hyperliquid funding history reached the local safety limit; narrow the requested time range.")
            complete = False
        for item in funding.records:
            occurred_at = milliseconds_to_datetime(item.get("time"), end)
            delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
            identity = f"funding:{item.get('hash')}:{delta.get('coin')}:{item.get('time')}"
            raw, created = self._raw(account, connection, identity, "funding", occurred_at, item)
            if not created:
                continue
            amount = decimal_value(delta.get("usdc"))
            if amount == 0:
                raw.status = RawEventStatus.IGNORED
                continue
            usdc = self._asset("USDC", "perp")
            self._ledger(raw, account, LedgerEventType.FUNDING, occurred_at, [(usdc, EntryDirection.CREDIT if amount > 0 else EntryDirection.DEBIT, abs(amount), False)], tx_hash=item.get("hash"), metadata={"market_type": "hyperliquid_perp", "symbol": delta.get("coin"), "funding_rate": delta.get("fundingRate"), "position_size": delta.get("szi")})

        updates = collector.ledger_updates(address, start, end)
        if updates.truncated:
            warnings.append("Hyperliquid ledger history reached the local safety limit; narrow the requested time range.")
            complete = False
        unsupported: set[str] = set()
        for item in updates.records:
            occurred_at = milliseconds_to_datetime(item.get("time"), end)
            delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
            delta_type = str(delta.get("type", "unknown"))
            identity = f"ledger:{item.get('hash') or self._hash(item)}:{delta_type}:{item.get('time')}"
            raw, created = self._raw(account, connection, identity, "ledger_update", occurred_at, item)
            if not created:
                continue
            normalized = self._normalize_ledger_update(address, delta)
            if normalized is None:
                raw.status = RawEventStatus.IGNORED
                unsupported.add(delta_type)
                continue
            event_type, legs, metadata = normalized
            self._ledger(raw, account, event_type, occurred_at, legs, tx_hash=item.get("hash"), metadata=metadata)
        if unsupported:
            warnings.append(f"Hyperliquid ledger update types retained as raw-only: {', '.join(sorted(unsupported))}.")
        return complete

    def _normalize_ledger_update(
        self,
        address: str,
        delta: dict[str, Any],
    ) -> tuple[LedgerEventType, list[tuple[Asset, EntryDirection, Decimal, bool]], dict[str, Any]] | None:
        delta_type = str(delta.get("type", ""))
        asset_symbol = str(delta.get("token") or delta.get("coin") or "USDC")
        amount = decimal_value(delta.get("usdc", delta.get("amount")))
        fee = decimal_value(delta.get("fee"))
        asset = self._asset(asset_symbol, "ledger")
        metadata = {"hyperliquid_delta_type": delta_type, "counterparty": delta.get("destination") or delta.get("user")}
        if delta_type == "deposit" and amount > 0:
            return LedgerEventType.DEPOSIT, [(asset, EntryDirection.CREDIT, amount, False)], metadata
        if delta_type == "withdraw" and amount != 0:
            legs = [(asset, EntryDirection.DEBIT, abs(amount), False)]
            if fee > 0:
                legs.append((self._asset("USDC", "ledger"), EntryDirection.DEBIT, fee, True))
            return LedgerEventType.WITHDRAW, legs, metadata
        if delta_type in {"internalTransfer", "spotTransfer"} and amount != 0:
            origin = str(delta.get("user", "")).lower()
            destination = str(delta.get("destination", "")).lower()
            if origin == address:
                event_type, direction = LedgerEventType.TRANSFER_OUT, EntryDirection.DEBIT
            elif destination == address:
                event_type, direction = LedgerEventType.TRANSFER_IN, EntryDirection.CREDIT
            else:
                return None
            legs = [(asset, direction, abs(amount), False)]
            if fee > 0 and direction == EntryDirection.DEBIT:
                legs.append((self._asset("USDC", "ledger"), EntryDirection.DEBIT, fee, True))
            return event_type, legs, metadata
        if delta_type == "subAccountTransfer" and amount != 0:
            master = str(delta.get("master", "")).lower()
            sub_account = str(delta.get("subAccount", "")).lower()
            is_deposit = bool(delta.get("isDeposit"))
            if address == master:
                direction = EntryDirection.DEBIT if is_deposit else EntryDirection.CREDIT
            elif address == sub_account:
                direction = EntryDirection.CREDIT if is_deposit else EntryDirection.DEBIT
            else:
                return None
            event_type = LedgerEventType.TRANSFER_IN if direction == EntryDirection.CREDIT else LedgerEventType.TRANSFER_OUT
            return event_type, [(asset, direction, abs(amount), False)], metadata
        if delta_type == "accountClassTransfer" and amount != 0:
            quantity = abs(amount)
            return LedgerEventType.INTERNAL_TRANSFER, [(asset, EntryDirection.DEBIT, quantity, False), (asset, EntryDirection.CREDIT, quantity, False)], metadata
        if delta_type == "rewardsClaim" and amount > 0:
            return LedgerEventType.INTEREST, [(asset, EntryDirection.CREDIT, amount, False)], metadata
        return None

    @staticmethod
    def _is_spot_fill(fill: dict[str, Any]) -> bool:
        direction = str(fill.get("dir", "")).strip().lower()
        coin = str(fill.get("coin", ""))
        return bool(coin) and (direction in {"buy", "sell"} or "/" in coin)

    @staticmethod
    def _spot_fill_pair(fill: dict[str, Any]) -> tuple[str, str]:
        coin = str(fill.get("coin", "")).strip()
        if "/" in coin:
            base, quote = coin.split("/", 1)
            if base and quote:
                return base, quote
        # Hyperliquid spot fills without an explicit pair are currently USDC-quoted.
        return coin, "USDC"

    def _asset(self, source_symbol: str, source_kind: str) -> Asset:
        source_symbol = source_symbol.strip()
        if not source_symbol:
            raise ValueError("Hyperliquid payload contains an empty asset symbol")
        canonical = source_symbol.upper()
        if len(canonical) > 32:
            canonical = f"{canonical[:23]}-{hashlib.sha256(canonical.encode()).hexdigest()[:8]}"
        asset = self.session.scalar(select(Asset).where(Asset.canonical_symbol == canonical, Asset.chain_id.is_(None), Asset.contract_address.is_(None)))
        if not asset:
            stablecoins = {"USDC", "USDT", "DAI", "USDE", "USDH"}
            asset = Asset(canonical_symbol=canonical, name=source_symbol[:160], asset_type=AssetType.STABLECOIN if canonical in stablecoins else AssetType.TOKEN, decimals=18)
            self.session.add(asset)
            self.session.flush()
        alias_source = f"hyperliquid:{source_kind}"
        alias = self.session.scalar(select(AssetAlias).where(AssetAlias.source == alias_source, AssetAlias.source_asset_id == source_symbol))
        if not alias:
            self.session.add(AssetAlias(asset_id=asset.id, source=alias_source, source_asset_id=source_symbol, symbol=source_symbol[:64]))
            self.session.flush()
        return asset

    def _raw(
        self,
        account: Account,
        connection: ApiConnection,
        external_id: str,
        kind: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> tuple[RawEvent, bool]:
        existing = self.session.scalar(select(RawEvent).where(RawEvent.account_id == account.id, RawEvent.source == "hyperliquid", RawEvent.external_event_id == external_id))
        if existing:
            self.stats.raw_existing += 1
            return existing, False
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        raw = RawEvent(account_id=account.id, connection_id=connection.id, source="hyperliquid", external_event_id=external_id[:256], event_kind=kind, occurred_at=occurred_at, payload_json=payload, payload_hash=hashlib.sha256(canonical.encode()).hexdigest(), status=RawEventStatus.RECEIVED)
        self.session.add(raw)
        self.session.flush()
        self.stats.raw_created += 1
        return raw, True

    def _public_raw(self, account: Account, connection: ApiConnection, kind: str, payload: dict[str, Any], as_of: datetime) -> None:
        day = as_of.astimezone(timezone.utc).strftime("%Y-%m-%d")
        self._raw(account, connection, f"{kind}:{day}", kind, as_of, payload)

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
        event = LedgerEvent(portfolio_id=account.portfolio_id, raw_event_id=raw.id, event_type=event_type, source=EventSource.RAW, status=EventStatus.POSTED, occurred_at=occurred_at, tx_hash=str(tx_hash) if tx_hash else None, external_reference=external_reference, metadata_json=metadata or {})
        self.session.add(event)
        self.session.flush()
        for asset, direction, quantity, fee_flag in valid_legs:
            self.session.add(LedgerEntry(ledger_event_id=event.id, account_id=account.id, asset_id=asset.id, direction=direction, quantity=quantity, fee_flag=fee_flag))
        raw.status = RawEventStatus.NORMALIZED
        self.stats.ledger_created += 1

    def _balance(self, account_id: UUID, asset_id: UUID, quantity: Decimal, as_of: datetime) -> None:
        if self.session.scalar(select(BalanceSnapshot.id).where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.asset_id == asset_id, BalanceSnapshot.as_of == as_of)):
            return
        self.session.add(BalanceSnapshot(account_id=account_id, asset_id=asset_id, quantity=quantity, source="hyperliquid:spot", as_of=as_of))
        self.stats.balances_created += 1

    def _price(self, asset_id: UUID, price_usd: Decimal, as_of: datetime, *, token_index: int) -> None:
        if price_usd <= 0 or self.session.scalar(
            select(AssetPrice.id).where(
                AssetPrice.asset_id == asset_id,
                AssetPrice.source == "hyperliquid:spot",
                AssetPrice.as_of == as_of,
            )
        ):
            return
        self.session.add(
            AssetPrice(
                asset_id=asset_id,
                price_usd=price_usd,
                source="hyperliquid:spot",
                as_of=as_of,
                metadata_json={"token_index": token_index},
            )
        )
        self.stats.prices_created += 1

    def _zero_missing_spot_balances(self, account_id: UUID, seen_asset_ids: set[UUID], as_of: datetime) -> None:
        prior_assets = set(
            self.session.scalars(
                select(BalanceSnapshot.asset_id).where(
                    BalanceSnapshot.account_id == account_id,
                    BalanceSnapshot.source == "hyperliquid:spot",
                )
            )
        )
        for asset_id in prior_assets - seen_asset_ids:
            self._balance(account_id, asset_id, Decimal("0"), as_of)

    def _incremental_start(self, account_id: UUID, fallback: datetime, explicit_start: bool) -> datetime:
        if explicit_start:
            return fallback
        cursor = self.session.scalar(select(SyncCursor).where(SyncCursor.account_id == account_id, SyncCursor.resource == "hyperliquid:account"))
        if not cursor or not cursor.last_synced_at:
            return fallback
        return max(fallback, cursor.last_synced_at - timedelta(minutes=5))

    def _update_cursor(self, account_id: UUID, end: datetime) -> None:
        cursor = self.session.scalar(select(SyncCursor).where(SyncCursor.account_id == account_id, SyncCursor.resource == "hyperliquid:account"))
        if not cursor:
            cursor = SyncCursor(account_id=account_id, resource="hyperliquid:account")
            self.session.add(cursor)
        cursor.cursor_value = str(int(end.timestamp() * 1000))
        cursor.last_synced_at = end

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
    def _mark_prices(market_context: list[Any]) -> dict[str, Decimal]:
        if len(market_context) != 2 or not isinstance(market_context[0], dict) or not isinstance(market_context[1], list):
            return {}
        universe = market_context[0].get("universe", [])
        result: dict[str, Decimal] = {}
        for market, context in zip(universe, market_context[1], strict=False):
            if isinstance(market, dict) and isinstance(context, dict) and market.get("name"):
                result[str(market["name"])] = decimal_value(context.get("markPx"))
        return result

    @staticmethod
    def _spot_prices(market_context: list[Any]) -> dict[int, Decimal]:
        if len(market_context) != 2 or not isinstance(market_context[0], dict) or not isinstance(market_context[1], list):
            return {}
        meta = market_context[0]
        token_names = {
            int(item["index"]): str(item["name"]).upper()
            for item in meta.get("tokens", [])
            if isinstance(item, dict) and item.get("index") is not None and item.get("name")
        }
        prices: dict[int, Decimal] = {
            index: Decimal("1")
            for index, symbol in token_names.items()
            if symbol in {"USD", "USDC", "USDT"}
        }
        pending: list[tuple[int, int, Decimal]] = []
        for market, context in zip(meta.get("universe", []), market_context[1], strict=False):
            if not isinstance(market, dict) or not isinstance(context, dict):
                continue
            tokens = market.get("tokens")
            if not isinstance(tokens, list) or len(tokens) != 2:
                continue
            try:
                base_index, quote_index = int(tokens[0]), int(tokens[1])
            except (TypeError, ValueError):
                continue
            pair_price = decimal_value(context.get("midPx")) or decimal_value(context.get("markPx"))
            if pair_price > 0:
                pending.append((base_index, quote_index, pair_price))
        while pending:
            unresolved: list[tuple[int, int, Decimal]] = []
            progressed = False
            for base_index, quote_index, pair_price in pending:
                quote_price = prices.get(quote_index)
                if quote_price is None:
                    unresolved.append((base_index, quote_index, pair_price))
                    continue
                prices.setdefault(base_index, pair_price * quote_price)
                progressed = True
            if not progressed:
                break
            pending = unresolved
        return prices

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
