import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountEquitySnapshot,
    ApiConnection,
    Asset,
    AssetAlias,
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
)


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


class CexSyncStats:
    def __init__(self) -> None:
        self.raw_created = 0
        self.raw_existing = 0
        self.ledger_created = 0
        self.balances_created = 0
        self.positions_created = 0
        self.equity_created = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_created": self.raw_created,
            "raw_existing": self.raw_existing,
            "ledger_created": self.ledger_created,
            "balances_created": self.balances_created,
            "positions_created": self.positions_created,
            "equity_created": self.equity_created,
        }


class CexLedgerWriter:
    """Append-only raw evidence and normalized snapshot writer shared by CEX connectors."""

    STABLECOINS = {"USDT", "USDC", "FDUSD", "DAI", "USDE", "USD1", "PYUSD"}

    def __init__(self, session: Session, provider: str, stats: CexSyncStats) -> None:
        self.session = session
        self.provider = provider
        self.stats = stats

    def asset(self, symbol: str, product: str) -> Asset:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError(f"{self.provider} payload contains an empty asset symbol")
        asset = self.session.scalar(
            select(Asset).where(
                Asset.canonical_symbol == symbol,
                Asset.chain_id.is_(None),
                Asset.contract_address.is_(None),
            )
        )
        if not asset:
            asset = Asset(
                canonical_symbol=symbol,
                name=symbol,
                asset_type=AssetType.STABLECOIN if symbol in self.STABLECOINS else AssetType.TOKEN,
                decimals=18,
            )
            self.session.add(asset)
            self.session.flush()
        alias_source = f"{self.provider}:{product}"
        alias = self.session.scalar(
            select(AssetAlias).where(
                AssetAlias.source == alias_source,
                AssetAlias.source_asset_id == symbol,
            )
        )
        if not alias:
            self.session.add(AssetAlias(asset_id=asset.id, source=alias_source, source_asset_id=symbol, symbol=symbol))
            self.session.flush()
        return asset

    def raw(
        self,
        account: Account,
        connection: ApiConnection,
        product: str,
        external_id: str,
        kind: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> tuple[RawEvent, bool]:
        source = f"{self.provider}:{product}"
        existing = self.session.scalar(
            select(RawEvent).where(
                RawEvent.account_id == account.id,
                RawEvent.source == source,
                RawEvent.external_event_id == external_id[:256],
            )
        )
        if existing:
            self.stats.raw_existing += 1
            return existing, False
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        raw = RawEvent(
            account_id=account.id,
            connection_id=connection.id,
            source=source,
            external_event_id=external_id[:256],
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

    def balance(self, account_id: UUID, asset_id: UUID, quantity: Decimal, product: str, as_of: datetime) -> None:
        exists = self.session.scalar(
            select(BalanceSnapshot.id).where(
                BalanceSnapshot.account_id == account_id,
                BalanceSnapshot.asset_id == asset_id,
                BalanceSnapshot.as_of == as_of,
            )
        )
        if exists:
            return
        self.session.add(
            BalanceSnapshot(
                account_id=account_id,
                asset_id=asset_id,
                quantity=quantity,
                source=f"{self.provider}:{product}",
                as_of=as_of,
            )
        )
        self.stats.balances_created += 1

    def equity(
        self,
        account: Account,
        raw: RawEvent,
        as_of: datetime,
        *,
        equity: Decimal,
        withdrawable: Decimal | None = None,
        margin_used: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        exists = self.session.scalar(
            select(AccountEquitySnapshot.id).where(
                AccountEquitySnapshot.account_id == account.id,
                AccountEquitySnapshot.provider == self.provider,
                AccountEquitySnapshot.as_of == as_of,
            )
        )
        if exists:
            return
        self.session.add(
            AccountEquitySnapshot(
                account_id=account.id,
                source_raw_event_id=raw.id,
                provider=self.provider,
                currency="USD",
                equity=equity,
                withdrawable=withdrawable,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl,
                as_of=as_of,
                metadata_json=metadata or {},
            )
        )
        self.stats.equity_created += 1

    def position(
        self,
        account: Account,
        raw: RawEvent,
        product: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        as_of: datetime,
        **values: Any,
    ) -> None:
        side = side.upper()
        exists = self.session.scalar(
            select(PositionSnapshot.id).where(
                PositionSnapshot.account_id == account.id,
                PositionSnapshot.product == product,
                PositionSnapshot.symbol == symbol,
                PositionSnapshot.position_side == side,
                PositionSnapshot.as_of == as_of,
            )
        )
        if exists:
            return
        self.session.add(
            PositionSnapshot(
                account_id=account.id,
                source_raw_event_id=raw.id,
                product=product,
                symbol=symbol,
                position_side=side,
                quantity=quantity,
                entry_price=values.get("entry_price"),
                mark_price=values.get("mark_price"),
                unrealized_pnl=values.get("unrealized_pnl"),
                leverage=values.get("leverage"),
                liquidation_price=values.get("liquidation_price"),
                notional=values.get("notional"),
                margin_asset=values.get("margin_asset"),
                isolated=bool(values.get("isolated", False)),
                as_of=as_of,
                metadata_json=values.get("metadata") or {},
            )
        )
        self.stats.positions_created += 1

    def ledger(
        self,
        raw: RawEvent,
        account: Account,
        event_type: LedgerEventType,
        occurred_at: datetime,
        legs: list[tuple[Asset, EntryDirection, Decimal, bool]],
        *,
        tx_hash: Any = None,
        external_reference: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        valid = [leg for leg in legs if leg[2] > 0]
        if not valid or self.session.scalar(select(LedgerEvent.id).where(LedgerEvent.raw_event_id == raw.id)):
            return
        event = LedgerEvent(
            portfolio_id=account.portfolio_id,
            raw_event_id=raw.id,
            event_type=event_type,
            source=EventSource.RAW,
            status=EventStatus.POSTED,
            occurred_at=occurred_at,
            tx_hash=str(tx_hash) if tx_hash else None,
            external_reference=str(external_reference) if external_reference else None,
            metadata_json=metadata or {},
        )
        self.session.add(event)
        self.session.flush()
        for asset, direction, quantity, fee_flag in valid:
            self.session.add(
                LedgerEntry(
                    ledger_event_id=event.id,
                    account_id=account.id,
                    asset_id=asset.id,
                    direction=direction,
                    quantity=quantity,
                    fee_flag=fee_flag,
                )
            )
        raw.status = RawEventStatus.NORMALIZED
        self.stats.ledger_created += 1
