import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


UUID = uuid.UUID
Money = Numeric(38, 18)


class AccountKind(str, enum.Enum):
    EXCHANGE = "exchange"
    WALLET = "wallet"
    PERP_DEX = "perp_dex"
    DEFI = "defi"
    MANUAL = "manual"


class AssetType(str, enum.Enum):
    NATIVE = "native"
    TOKEN = "token"
    STABLECOIN = "stablecoin"
    FIAT = "fiat"
    DERIVATIVE = "derivative"


class LedgerEventType(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    INTERNAL_TRANSFER = "internal_transfer"
    SWAP = "swap"
    FUNDING = "funding"
    FEE = "fee"
    AIRDROP = "airdrop"
    STAKING_REWARD = "staking_reward"
    INTEREST = "interest"
    BORROW = "borrow"
    REPAY = "repay"
    LIQUIDATION = "liquidation"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class EventSource(str, enum.Enum):
    RAW = "raw"
    MANUAL = "manual"
    SYSTEM = "system"


class EventStatus(str, enum.Enum):
    PENDING = "pending"
    POSTED = "posted"
    IGNORED = "ignored"
    ERROR = "error"


class EntryDirection(str, enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class RawEventStatus(str, enum.Enum):
    RECEIVED = "received"
    NORMALIZED = "normalized"
    FAILED = "failed"
    IGNORED = "ignored"


class SyncRunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class TransferCandidateStatus(str, enum.Enum):
    UNMATCHED = "unmatched"
    NEEDS_REVIEW = "needs_review"
    AUTO_MATCHED = "auto_matched"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    IGNORED = "ignored"


class TransferGroupStatus(str, enum.Enum):
    AUTO_MATCHED = "auto_matched"
    CONFIRMED = "confirmed"
    UNMATCHED = "unmatched"


class CostMethod(str, enum.Enum):
    AVERAGE_COST = "average_cost"
    FIFO = "fifo"
    LIFO = "lifo"


class CostOverrideType(str, enum.Enum):
    EVENT_TOTAL = "event_total"
    POSITION_TOTAL = "position_total"


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    base_currency: Mapped[str] = mapped_column(String(16), default="USD")
    default_cost_method: Mapped[str] = mapped_column(String(32), default="average_cost")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        Index(
            "ux_asset_identity",
            "canonical_symbol",
            "chain_id",
            "contract_address",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("decimals >= 0 AND decimals <= 36", name="asset_decimals_range"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_symbol: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(160))
    asset_type: Mapped[AssetType] = mapped_column(Enum(AssetType, native_enum=False), default=AssetType.TOKEN)
    decimals: Mapped[int] = mapped_column(Integer, default=18)
    chain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contract_address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    underlying_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AssetAlias(Base):
    __tablename__ = "asset_aliases"
    __table_args__ = (UniqueConstraint("source", "source_asset_id", name="source_asset_identity"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_asset_id: Mapped[str] = mapped_column(String(256))
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("portfolio_id", "provider", "external_account_id", name="account_source_identity"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    kind: Mapped[AccountKind] = mapped_column(Enum(AccountKind, native_enum=False))
    provider: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(120))
    external_account_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    chain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EvmTrackedContract(Base):
    __tablename__ = "evm_tracked_contracts"
    __table_args__ = (
        UniqueConstraint("account_id", "contract_address", name="evm_tracked_contract_identity"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    contract_address: Mapped[str] = mapped_column(String(42))
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ApiConnection(Base):
    __tablename__ = "api_connections"
    __table_args__ = (UniqueConstraint("account_id", "name", name="connection_name_per_account"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(64))
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    encrypted_api_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_passphrase: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_permissions: Mapped[list] = mapped_column(JSON, default=lambda: ["read"])
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ConnectionMarketScope(Base):
    __tablename__ = "connection_market_scopes"
    __table_args__ = (
        UniqueConstraint("connection_id", "product", "symbol", name="connection_market_scope_identity"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("api_connections.id"), index=True)
    product: Mapped[str] = mapped_column(String(16), default="spot")
    symbol: Mapped[str] = mapped_column(String(64))
    discovery_source: Mapped[str] = mapped_column(String(32), default="manual")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("account_id", "source", "external_event_id", name="raw_source_event_identity"),
        Index("ix_raw_events_account_occurred", "account_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    connection_id: Mapped[UUID | None] = mapped_column(ForeignKey("api_connections.id"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(64))
    external_event_id: Mapped[str] = mapped_column(String(256))
    event_kind: Mapped[str] = mapped_column(String(96))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[RawEventStatus] = mapped_column(Enum(RawEventStatus, native_enum=False), default=RawEventStatus.RECEIVED)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LedgerEvent(Base):
    __tablename__ = "ledger_events"
    __table_args__ = (Index("ix_ledger_events_portfolio_occurred", "portfolio_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    raw_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_events.id"), unique=True, nullable=True)
    event_type: Mapped[LedgerEventType] = mapped_column(Enum(LedgerEventType, native_enum=False), index=True)
    source: Mapped[EventSource] = mapped_column(Enum(EventSource, native_enum=False))
    status: Mapped[EventStatus] = mapped_column(Enum(EventStatus, native_enum=False), default=EventStatus.PENDING)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    external_reference: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ledger_entry_positive_quantity"),
        Index("ix_ledger_entries_account_asset", "account_id", "asset_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ledger_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"), index=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    direction: Mapped[EntryDirection] = mapped_column(Enum(EntryDirection, native_enum=False))
    quantity: Mapped[Decimal] = mapped_column(Money)
    unit_price_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fee_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "asset_id", "as_of", name="balance_snapshot_identity"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AccountEquitySnapshot(Base):
    __tablename__ = "account_equity_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "provider", "as_of", name="account_equity_snapshot_identity"),
        Index("ix_account_equity_snapshots_account_as_of", "account_id", "as_of"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    source_raw_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(32), default="USD")
    equity: Mapped[Decimal] = mapped_column(Money)
    withdrawable: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    margin_used: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    total_notional: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (UniqueConstraint("account_id", "resource", name="sync_cursor_resource"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    resource: Mapped[str] = mapped_column(String(96))
    cursor_value: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "product", "symbol", "position_side", "as_of", name="position_snapshot_identity"),
        Index("ix_position_snapshots_account_as_of", "account_id", "as_of"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    source_raw_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_events.id"), nullable=True)
    product: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    position_side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(Money)
    entry_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    leverage: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    liquidation_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    notional: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    margin_asset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    isolated: Mapped[bool] = mapped_column(Boolean, default=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_connection_started", "connection_id", "started_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("api_connections.id"), index=True)
    status: Mapped[SyncRunStatus] = mapped_column(Enum(SyncRunStatus, native_enum=False), default=SyncRunStatus.RUNNING)
    requested_products: Mapped[list] = mapped_column(JSON, default=list)
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WalletSyncRun(Base):
    __tablename__ = "wallet_sync_runs"
    __table_args__ = (Index("ix_wallet_sync_runs_account_started", "account_id", "started_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    chain_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[SyncRunStatus] = mapped_column(Enum(SyncRunStatus, native_enum=False), default=SyncRunStatus.RUNNING)
    from_block: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_block: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_confirmed_block: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    failed_ranges_json: Mapped[list] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TransferMatchRun(Base):
    __tablename__ = "transfer_match_runs"
    __table_args__ = (Index("ix_transfer_match_runs_portfolio_started", "portfolio_id", "started_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    status: Mapped[SyncRunStatus] = mapped_column(Enum(SyncRunStatus, native_enum=False), default=SyncRunStatus.RUNNING)
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TransferCandidate(Base):
    __tablename__ = "transfer_candidates"
    __table_args__ = (
        UniqueConstraint("source_event_id", "destination_event_id", name="transfer_candidate_event_pair"),
        CheckConstraint("source_event_id <> destination_event_id", name="transfer_candidate_distinct_events"),
        CheckConstraint("score >= 0 AND score <= 100", name="transfer_candidate_score_range"),
        Index("ix_transfer_candidates_portfolio_status", "portfolio_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    source_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"), index=True)
    destination_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"), index=True)
    source_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    destination_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    source_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    destination_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    source_amount: Mapped[Decimal] = mapped_column(Money)
    destination_amount: Mapped[Decimal] = mapped_column(Money)
    estimated_fee_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    score: Mapped[int] = mapped_column(Integer)
    score_breakdown_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[TransferCandidateStatus] = mapped_column(
        Enum(TransferCandidateStatus, native_enum=False), default=TransferCandidateStatus.UNMATCHED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class TransferGroup(Base):
    __tablename__ = "transfer_groups"
    __table_args__ = (
        CheckConstraint("source_event_id <> destination_event_id", name="transfer_group_distinct_events"),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 100", name="transfer_group_score_range"),
        Index("ix_transfer_groups_portfolio_status", "portfolio_id", "status"),
        Index("ix_transfer_groups_source_event", "source_event_id"),
        Index("ix_transfer_groups_destination_event", "destination_event_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("transfer_candidates.id"), nullable=True, index=True)
    source_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"))
    destination_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"))
    source_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    destination_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    source_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    destination_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    source_amount: Mapped[Decimal] = mapped_column(Money)
    destination_amount: Mapped[Decimal] = mapped_column(Money)
    fee_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fee_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    withdrawal_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    deposit_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    destination_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    original_cost_basis: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    status: Mapped[TransferGroupStatus] = mapped_column(Enum(TransferGroupStatus, native_enum=False))
    confidence_score: Mapped[int] = mapped_column(Integer)
    match_method: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AssetPrice(Base):
    __tablename__ = "asset_prices"
    __table_args__ = (
        CheckConstraint("price_usd > 0", name="asset_price_positive"),
        Index("ix_asset_prices_asset_as_of", "asset_id", "as_of"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    price_usd: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CostBasisOverride(Base):
    __tablename__ = "cost_basis_overrides"
    __table_args__ = (
        CheckConstraint("total_cost_usd >= 0", name="cost_override_nonnegative"),
        Index("ix_cost_overrides_portfolio_asset_created", "portfolio_id", "asset_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    ledger_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("ledger_events.id"), nullable=True, index=True)
    override_type: Mapped[CostOverrideType] = mapped_column(Enum(CostOverrideType, native_enum=False))
    total_cost_usd: Mapped[Decimal] = mapped_column(Money)
    reason: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class CostBasisRun(Base):
    __tablename__ = "cost_basis_runs"
    __table_args__ = (Index("ix_cost_basis_runs_portfolio_started", "portfolio_id", "started_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    method: Mapped[CostMethod] = mapped_column(Enum(CostMethod, native_enum=False), default=CostMethod.AVERAGE_COST)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[SyncRunStatus] = mapped_column(Enum(SyncRunStatus, native_enum=False), default=SyncRunStatus.RUNNING)
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CostLot(Base):
    __tablename__ = "cost_lots"
    __table_args__ = (
        CheckConstraint("original_quantity > 0", name="cost_lot_positive_original_quantity"),
        CheckConstraint("remaining_quantity >= 0", name="cost_lot_nonnegative_remaining_quantity"),
        Index("ix_cost_lots_run_account_asset", "run_id", "account_id", "asset_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("cost_basis_runs.id"), index=True)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    origin_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("ledger_events.id"), nullable=True, index=True)
    origin_entry_id: Mapped[UUID | None] = mapped_column(ForeignKey("ledger_entries.id"), nullable=True)
    parent_lot_id: Mapped[UUID | None] = mapped_column(ForeignKey("cost_lots.id"), nullable=True)
    transfer_group_id: Mapped[UUID | None] = mapped_column(ForeignKey("transfer_groups.id"), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    original_quantity: Mapped[Decimal] = mapped_column(Money)
    remaining_quantity: Mapped[Decimal] = mapped_column(Money)
    original_cost_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    remaining_cost_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    cost_known: Mapped[bool] = mapped_column(Boolean, default=True)
    acquisition_type: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CostLotConsumption(Base):
    __tablename__ = "cost_lot_consumptions"
    __table_args__ = (Index("ix_cost_consumptions_run_event", "run_id", "ledger_event_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("cost_basis_runs.id"), index=True)
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("cost_lots.id"), index=True)
    ledger_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"), index=True)
    ledger_entry_id: Mapped[UUID | None] = mapped_column(ForeignKey("ledger_entries.id"), nullable=True)
    transfer_group_id: Mapped[UUID | None] = mapped_column(ForeignKey("transfer_groups.id"), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    cost_basis_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    disposition_type: Mapped[str] = mapped_column(String(64))
    realizes_pnl: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RealizedPnlRecord(Base):
    __tablename__ = "realized_pnl_records"
    __table_args__ = (Index("ix_realized_pnl_run_occurred", "run_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("cost_basis_runs.id"), index=True)
    ledger_event_id: Mapped[UUID] = mapped_column(ForeignKey("ledger_events.id"), index=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    proceeds_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    cost_basis_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fee_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class PositionCostSnapshot(Base):
    __tablename__ = "position_cost_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "account_id", "asset_id", name="position_cost_run_account_asset"),
        Index("ix_position_cost_portfolio_asset", "portfolio_id", "asset_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("cost_basis_runs.id"), index=True)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    calculated_cost_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    manual_cost_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    effective_cost_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    average_unit_cost_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    market_price_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    market_value_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unrealized_pnl_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unrealized_pnl_percent: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PnlAdjustment(Base):
    __tablename__ = "pnl_adjustments"
    __table_args__ = (Index("ix_pnl_adjustments_portfolio_occurred", "portfolio_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    amount_usd: Mapped[Decimal] = mapped_column(Money)
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "as_of", name="portfolio_snapshot_identity"),
        Index("ix_portfolio_snapshots_portfolio_as_of", "portfolio_id", "as_of"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    source_cost_run_id: Mapped[UUID] = mapped_column(ForeignKey("cost_basis_runs.id"), index=True)
    total_nav: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    spot_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    perp_equity: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    defi_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cash: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    debt: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fee_expense: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    funding_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    external_flow: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    investment_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    valuation_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    data_quality_json: Mapped[dict] = mapped_column(JSON, default=dict)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    registration_slot: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_last_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AuthSession(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_sensitive_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class LoginChallenge(Base):
    __tablename__ = "login_challenges"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    remember_me: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (UniqueConstraint("scope", "identifier_hash", name="login_attempt_scope_identifier"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16))
    identifier_hash: Mapped[str] = mapped_column(String(64), index=True)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_user_created", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(96), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
