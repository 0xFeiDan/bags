import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import (
    AccountKind,
    AssetType,
    CostMethod,
    CostOverrideType,
    EntryDirection,
    EventSource,
    EventStatus,
    LedgerEventType,
    RawEventStatus,
    SyncRunStatus,
    TransferCandidateStatus,
    TransferGroupStatus,
)


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="USD", min_length=2, max_length=16)

    @field_validator("base_currency")
    @classmethod
    def only_supported_base_currency(cls, value: str) -> str:
        if value.upper() != "USD":
            raise ValueError("Phase 1-7 currently supports USD portfolios only")
        return "USD"


class PortfolioRead(Schema):
    id: UUID
    name: str
    base_currency: str
    default_cost_method: str
    created_at: datetime


class AssetCreate(BaseModel):
    canonical_symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    asset_type: AssetType = AssetType.TOKEN
    decimals: int = Field(default=18, ge=0, le=36)
    chain_id: str | None = Field(default=None, max_length=64)
    contract_address: str | None = Field(default=None, max_length=256)
    underlying_asset_id: UUID | None = None


class AssetRead(Schema):
    id: UUID
    canonical_symbol: str
    name: str
    asset_type: AssetType
    decimals: int
    chain_id: str | None
    contract_address: str | None
    underlying_asset_id: UUID | None
    is_active: bool
    created_at: datetime


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: UUID
    kind: AccountKind
    provider: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    external_account_id: str | None = Field(default=None, max_length=256)
    chain_id: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=256)


class AccountRead(Schema):
    id: UUID
    portfolio_id: UUID
    kind: AccountKind
    provider: str
    label: str
    external_account_id: str | None
    chain_id: str | None
    address: str | None
    is_active: bool
    created_at: datetime


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: UUID
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=64)
    api_key: str | None = Field(default=None, min_length=1, max_length=2048)
    api_secret: str | None = Field(default=None, max_length=4096)
    passphrase: str | None = Field(default=None, max_length=4096)
    requested_permissions: list[str] = Field(default_factory=lambda: ["read"])

    @field_validator("requested_permissions")
    @classmethod
    def read_only(cls, permissions: list[str]) -> list[str]:
        normalized = sorted({permission.strip().lower() for permission in permissions if permission.strip()})
        if normalized != ["read"]:
            raise ValueError("only the read permission is permitted")
        return normalized


class ConnectionRead(Schema):
    id: UUID
    account_id: UUID
    name: str
    provider: str
    api_key_hint: str
    requested_permissions: list[str]
    is_enabled: bool
    created_at: datetime


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    api_key: str | None = Field(default=None, min_length=1, max_length=2048)
    api_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    passphrase: str | None = Field(default=None, min_length=1, max_length=4096)
    is_enabled: bool | None = None

    @model_validator(mode="after")
    def has_update(self):
        if all(
            value is None
            for value in (self.name, self.api_key, self.api_secret, self.passphrase, self.is_enabled)
        ):
            raise ValueError("at least one connection field must be updated")
        return self


class RawEventCreate(BaseModel):
    account_id: UUID | None = None
    connection_id: UUID | None = None
    source: str = Field(min_length=1, max_length=64)
    external_event_id: str = Field(min_length=1, max_length=256)
    event_kind: str = Field(min_length=1, max_length=96)
    occurred_at: datetime
    payload_json: dict[str, Any]

    @field_validator("payload_json")
    @classmethod
    def bounded_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("payload_json must be serializable JSON") from error
        if len(encoded) > 256 * 1024:
            raise ValueError("payload_json must not exceed 256 KiB")

        def depth(value: Any, current: int = 0) -> int:
            if current > 16:
                return current
            if isinstance(value, dict):
                return max([current, *(depth(item, current + 1) for item in value.values())])
            if isinstance(value, list):
                return max([current, *(depth(item, current + 1) for item in value)])
            return current

        if depth(payload) > 16:
            raise ValueError("payload_json nesting must not exceed 16 levels")
        return payload


class RawEventRead(Schema):
    id: UUID
    account_id: UUID | None
    connection_id: UUID | None
    source: str
    external_event_id: str
    event_kind: str
    occurred_at: datetime
    payload_json: dict[str, Any]
    payload_hash: str
    status: RawEventStatus
    received_at: datetime


class LedgerEntryCreate(BaseModel):
    account_id: UUID
    asset_id: UUID
    direction: EntryDirection
    quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    unit_price_usd: Decimal | None = Field(default=None, ge=0, max_digits=38, decimal_places=18)
    fee_flag: bool = False
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class LedgerEventCreate(BaseModel):
    portfolio_id: UUID
    raw_event_id: UUID | None = None
    event_type: LedgerEventType
    source: EventSource = EventSource.MANUAL
    status: EventStatus = EventStatus.PENDING
    occurred_at: datetime
    tx_hash: str | None = Field(default=None, max_length=256)
    external_reference: str | None = Field(default=None, max_length=256)
    note: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    entries: list[LedgerEntryCreate] = Field(min_length=1, max_length=32)


class LedgerEntryRead(Schema):
    id: UUID
    account_id: UUID
    asset_id: UUID
    direction: EntryDirection
    quantity: Decimal
    unit_price_usd: Decimal | None
    fee_flag: bool
    metadata_json: dict[str, Any]


class LedgerEventRead(Schema):
    id: UUID
    portfolio_id: UUID
    raw_event_id: UUID | None
    event_type: LedgerEventType
    source: EventSource
    status: EventStatus
    occurred_at: datetime
    tx_hash: str | None
    external_reference: str | None
    note: str | None
    metadata_json: dict[str, Any]
    created_at: datetime
    entries: list[LedgerEntryRead]


class BalanceSnapshotCreate(BaseModel):
    account_id: UUID
    asset_id: UUID
    quantity: Decimal = Field(max_digits=38, decimal_places=18)
    source: str = Field(min_length=1, max_length=64)
    as_of: datetime


class BalanceSnapshotRead(Schema):
    id: UUID
    account_id: UUID
    asset_id: UUID
    quantity: Decimal
    source: str
    as_of: datetime
    received_at: datetime


BinanceProduct = Literal["spot", "usdm", "coinm"]


class BinanceSyncRequest(BaseModel):
    products: list[BinanceProduct] = Field(default_factory=lambda: ["spot", "usdm", "coinm"], min_length=1)
    spot_symbols: list[str] = Field(default_factory=list, max_length=200)
    usdm_symbols: list[str] = Field(default_factory=list, max_length=200)
    coinm_pairs: list[str] = Field(default_factory=list, max_length=100)
    history_start: datetime | None = None
    history_end: datetime | None = None

    @field_validator("products")
    @classmethod
    def unique_products(cls, products: list[BinanceProduct]) -> list[BinanceProduct]:
        return list(dict.fromkeys(products))

    @field_validator("spot_symbols", "usdm_symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        return list(dict.fromkeys(normalized))

    @field_validator("coinm_pairs")
    @classmethod
    def normalize_pairs(cls, pairs: list[str]) -> list[str]:
        normalized = [pair.strip().upper() for pair in pairs if pair.strip()]
        return list(dict.fromkeys(normalized))


class BinanceSyncRead(Schema):
    id: UUID
    connection_id: UUID
    status: SyncRunStatus
    requested_products: list[str]
    stats_json: dict[str, Any]
    warnings_json: list[str]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class BybitSyncRequest(BaseModel):
    products: list[Literal["spot", "linear", "inverse"]] = Field(
        default_factory=lambda: ["spot", "linear", "inverse"], min_length=1
    )
    spot_symbols: list[str] = Field(default_factory=list, max_length=200)
    linear_settle_coins: list[str] = Field(default_factory=lambda: ["USDT", "USDC"], max_length=20)
    inverse_settle_coins: list[str] = Field(default_factory=lambda: ["BTC", "ETH"], max_length=20)
    history_start: datetime | None = None
    history_end: datetime | None = None

    @field_validator("products")
    @classmethod
    def unique_bybit_products(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("spot_symbols", "linear_settle_coins", "inverse_settle_coins")
    @classmethod
    def normalize_bybit_symbols(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


class BitgetSyncRequest(BaseModel):
    products: list[Literal["spot", "usdt-futures", "usdc-futures", "coin-futures"]] = Field(
        default_factory=lambda: ["spot", "usdt-futures", "usdc-futures", "coin-futures"], min_length=1
    )
    spot_symbols: list[str] = Field(default_factory=list, max_length=200)
    history_start: datetime | None = None
    history_end: datetime | None = None

    @field_validator("products")
    @classmethod
    def unique_bitget_products(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("spot_symbols")
    @classmethod
    def normalize_bitget_symbols(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


class HyperliquidSyncRequest(BaseModel):
    history_start: datetime | None = None
    history_end: datetime | None = None
    include_spot: bool = True


class SyncRunRead(Schema):
    id: UUID
    connection_id: UUID
    status: SyncRunStatus
    requested_products: list[str]
    stats_json: dict[str, Any]
    warnings_json: list[str]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class AccountEquitySnapshotRead(Schema):
    id: UUID
    account_id: UUID
    provider: str
    currency: str
    equity: Decimal
    withdrawable: Decimal | None
    margin_used: Decimal | None
    total_notional: Decimal | None
    unrealized_pnl: Decimal | None
    as_of: datetime
    metadata_json: dict[str, Any]


class PositionSnapshotRead(Schema):
    id: UUID
    account_id: UUID
    product: str
    symbol: str
    position_side: str
    quantity: Decimal
    entry_price: Decimal | None
    mark_price: Decimal | None
    unrealized_pnl: Decimal | None
    leverage: Decimal | None
    liquidation_price: Decimal | None
    notional: Decimal | None
    margin_asset: str | None
    isolated: bool
    as_of: datetime
    metadata_json: dict[str, Any]


class EvmSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_block: int | None = Field(default=None, ge=0)
    to_block: int | None = Field(default=None, ge=0)
    token_contracts: list[str] = Field(default_factory=list, max_length=500)
    transaction_hashes: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("token_contracts")
    @classmethod
    def validate_contracts(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))
        invalid = [value for value in normalized if len(value) != 42 or not value.startswith("0x")]
        if invalid:
            raise ValueError("token contracts must be 42-character EVM addresses")
        for value in normalized:
            try:
                int(value[2:], 16)
            except ValueError as error:
                raise ValueError("token contracts must be hexadecimal EVM addresses") from error
        return normalized

    @field_validator("transaction_hashes")
    @classmethod
    def validate_hashes(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))
        invalid = [value for value in normalized if len(value) != 66 or not value.startswith("0x")]
        if invalid:
            raise ValueError("transaction hashes must be 32-byte hexadecimal values")
        for value in normalized:
            try:
                int(value[2:], 16)
            except ValueError as error:
                raise ValueError("transaction hashes must be hexadecimal values") from error
        return normalized


class WalletSyncRunRead(Schema):
    id: UUID
    account_id: UUID
    chain_id: str
    status: SyncRunStatus
    from_block: int | None
    to_block: int | None
    latest_confirmed_block: int | None
    stats_json: dict[str, Any]
    warnings_json: list[Any]
    failed_ranges_json: list[Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class EvmChainRead(BaseModel):
    key: str
    chain_id: str
    name: str
    native_symbol: str
    configured: bool


class TransferMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history_start: datetime | None = None
    history_end: datetime | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if self.history_start and self.history_end and self.history_start >= self.history_end:
            raise ValueError("history_start must be before history_end")
        return self


class TransferMatchRunRead(Schema):
    id: UUID
    portfolio_id: UUID
    status: SyncRunStatus
    stats_json: dict[str, Any]
    warnings_json: list[Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class TransferCandidateRead(Schema):
    id: UUID
    portfolio_id: UUID
    source_event_id: UUID
    destination_event_id: UUID
    source_account_id: UUID
    destination_account_id: UUID
    source_asset_id: UUID
    destination_asset_id: UUID
    source_amount: Decimal
    destination_amount: Decimal
    estimated_fee_amount: Decimal
    score: int
    score_breakdown_json: dict[str, Any]
    status: TransferCandidateStatus
    created_at: datetime
    updated_at: datetime


class TransferGroupRead(Schema):
    id: UUID
    reference: str
    portfolio_id: UUID
    candidate_id: UUID | None
    source_event_id: UUID
    destination_event_id: UUID
    source_account_id: UUID
    destination_account_id: UUID
    source_asset_id: UUID
    destination_asset_id: UUID
    source_amount: Decimal
    destination_amount: Decimal
    fee_amount: Decimal
    fee_asset_id: UUID | None
    tx_hash: str | None
    withdrawal_id: str | None
    deposit_id: str | None
    source_occurred_at: datetime
    destination_occurred_at: datetime
    original_cost_basis: Decimal | None
    status: TransferGroupStatus
    confidence_score: int
    match_method: str
    note: str | None
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ManualTransferMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_event_id: UUID
    destination_event_id: UUID
    note: str | None = Field(default=None, max_length=1000)


class TransferActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)


class CostBasisRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: CostMethod | None = None
    as_of: datetime | None = None


class CostBasisRunRead(Schema):
    id: UUID
    portfolio_id: UUID
    method: CostMethod
    as_of: datetime
    status: SyncRunStatus
    stats_json: dict[str, Any]
    warnings_json: list[Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class CostLotRead(Schema):
    id: UUID
    run_id: UUID
    portfolio_id: UUID
    account_id: UUID
    asset_id: UUID
    origin_event_id: UUID | None
    origin_entry_id: UUID | None
    parent_lot_id: UUID | None
    transfer_group_id: UUID | None
    acquired_at: datetime
    original_quantity: Decimal
    remaining_quantity: Decimal
    original_cost_usd: Decimal | None
    remaining_cost_usd: Decimal | None
    cost_known: bool
    acquisition_type: str
    metadata_json: dict[str, Any]
    created_at: datetime


class CostLotConsumptionRead(Schema):
    id: UUID
    run_id: UUID
    lot_id: UUID
    ledger_event_id: UUID
    ledger_entry_id: UUID | None
    transfer_group_id: UUID | None
    quantity: Decimal
    cost_basis_usd: Decimal | None
    disposition_type: str
    realizes_pnl: bool
    occurred_at: datetime


class RealizedPnlRead(Schema):
    id: UUID
    run_id: UUID
    ledger_event_id: UUID
    account_id: UUID
    asset_id: UUID
    category: str
    quantity: Decimal | None
    proceeds_usd: Decimal | None
    cost_basis_usd: Decimal | None
    fee_usd: Decimal
    realized_pnl_usd: Decimal | None
    occurred_at: datetime
    metadata_json: dict[str, Any]


class PositionCostRead(Schema):
    id: UUID
    run_id: UUID
    portfolio_id: UUID
    account_id: UUID
    asset_id: UUID
    quantity: Decimal
    calculated_cost_usd: Decimal | None
    manual_cost_usd: Decimal | None
    effective_cost_usd: Decimal | None
    average_unit_cost_usd: Decimal | None
    market_price_usd: Decimal | None
    market_value_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    unrealized_pnl_percent: Decimal | None
    as_of: datetime


class AssetCostSummaryRead(BaseModel):
    run_id: UUID
    portfolio_id: UUID
    asset_id: UUID
    symbol: str
    quantity: Decimal
    calculated_cost_usd: Decimal | None
    manual_cost_usd: Decimal | None
    effective_cost_usd: Decimal | None
    average_unit_cost_usd: Decimal | None
    market_price_usd: Decimal | None
    market_value_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    unrealized_pnl_percent: Decimal | None
    realized_pnl_usd: Decimal | None


class CostBasisOverrideCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: UUID
    asset_id: UUID
    account_id: UUID | None = None
    ledger_event_id: UUID | None = None
    override_type: CostOverrideType
    total_cost_usd: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    reason: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def valid_target(self):
        if self.override_type == CostOverrideType.EVENT_TOTAL and not self.ledger_event_id:
            raise ValueError("event_total requires ledger_event_id")
        if self.override_type == CostOverrideType.POSITION_TOTAL and self.ledger_event_id:
            raise ValueError("position_total cannot target a ledger event")
        return self


class CostBasisOverrideRead(Schema):
    id: UUID
    portfolio_id: UUID
    asset_id: UUID
    account_id: UUID | None
    ledger_event_id: UUID | None
    override_type: CostOverrideType
    total_cost_usd: Decimal
    reason: str
    created_by_user_id: UUID | None
    created_at: datetime


class AssetPriceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID
    price_usd: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    source: str = Field(default="manual", min_length=1, max_length=64)
    as_of: datetime
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AssetPriceRead(Schema):
    id: UUID
    asset_id: UUID
    price_usd: Decimal
    source: str
    as_of: datetime
    metadata_json: dict[str, Any]
    created_at: datetime


class PnlAdjustmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: UUID
    account_id: UUID | None = None
    asset_id: UUID | None = None
    amount_usd: Decimal = Field(max_digits=38, decimal_places=18)
    reason: str = Field(min_length=3, max_length=2000)
    occurred_at: datetime


class PnlAdjustmentRead(Schema):
    id: UUID
    portfolio_id: UUID
    account_id: UUID | None
    asset_id: UUID | None
    amount_usd: Decimal
    reason: str
    occurred_at: datetime
    created_by_user_id: UUID | None
    created_at: datetime


class PnlSummaryRead(BaseModel):
    run_id: UUID
    portfolio_id: UUID
    system_realized_pnl_usd: Decimal | None
    adjustment_usd: Decimal
    final_realized_pnl_usd: Decimal | None
    incomplete_records: int


class DashboardSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime | None = None
    method: CostMethod | None = None
    recalculate_cost: bool = True


class DashboardBackfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history_start: datetime
    history_end: datetime
    method: CostMethod | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if self.history_start > self.history_end:
            raise ValueError("history_start must be before history_end")
        if (self.history_end - self.history_start).days > 366:
            raise ValueError("dashboard backfill is limited to 366 days")
        return self


class PortfolioSnapshotRead(Schema):
    id: UUID
    portfolio_id: UUID
    source_cost_run_id: UUID
    total_nav: Decimal | None
    spot_value: Decimal
    perp_equity: Decimal
    defi_value: Decimal
    cash: Decimal
    debt: Decimal
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    fee_expense: Decimal | None
    funding_pnl: Decimal | None
    external_flow: Decimal | None
    investment_pnl: Decimal | None
    valuation_complete: bool
    data_quality_json: dict[str, Any]
    as_of: datetime
    created_at: datetime


class DashboardBackfillRead(BaseModel):
    portfolio_id: UUID
    created: int
    skipped_existing: int
    partial: int
    snapshot_ids: list[UUID]


class DashboardPeriodRead(BaseModel):
    key: str
    label: str
    pnl_usd: Decimal | None
    return_percent: Decimal | None
    external_flow_usd: Decimal | None
    baseline_nav_usd: Decimal | None
    baseline_as_of: datetime | None
    complete: bool


class DashboardAllocationRead(BaseModel):
    key: str
    label: str
    value_usd: Decimal
    percentage: Decimal | None


class DashboardAssetRead(BaseModel):
    asset_id: UUID
    symbol: str
    name: str
    quantity: Decimal
    calculated_cost_usd: Decimal | None
    manual_cost_usd: Decimal | None
    effective_cost_usd: Decimal | None
    average_cost_usd: Decimal | None
    current_price_usd: Decimal | None
    market_value_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    unrealized_pnl_percent: Decimal | None
    realized_pnl_usd: Decimal | None
    account_count: int
    open_lot_count: int
    valuation_complete: bool


class DashboardAccountRead(BaseModel):
    account_id: UUID
    label: str
    provider: str
    kind: str
    chain_id: str | None
    spot_value_usd: Decimal
    cash_usd: Decimal
    perp_equity_usd: Decimal
    total_equity_usd: Decimal | None
    realized_pnl_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    funding_pnl_usd: Decimal | None
    fee_expense_usd: Decimal | None
    margin_used_usd: Decimal | None
    last_synced_at: datetime | None
    valuation_complete: bool


class DashboardPositionRead(BaseModel):
    account_id: UUID
    account_label: str
    product: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal | None
    mark_price: Decimal | None
    notional_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    leverage: Decimal | None
    margin_usd: Decimal | None
    liquidation_price: Decimal | None
    as_of: datetime


class DashboardExposureRead(BaseModel):
    asset_id: UUID | None
    symbol: str
    spot_quantity: Decimal
    perp_long_quantity: Decimal
    perp_short_quantity: Decimal
    net_quantity: Decimal
    gross_long_usd: Decimal
    gross_short_usd: Decimal
    net_exposure_usd: Decimal


class DashboardHealthRead(BaseModel):
    cost_coverage_percent: Decimal | None
    valued_positions: int
    total_positions: int
    pending_transfer_reviews: int
    unknown_deposits: int
    incomplete_pnl_records: int
    balance_difference_count: int
    max_balance_difference_percent: Decimal | None
    valuation_complete: bool
    warnings: list[str]


class DashboardSummaryRead(BaseModel):
    portfolio_id: UUID
    portfolio_name: str
    base_currency: str
    cost_run_id: UUID
    cost_method: CostMethod
    as_of: datetime
    total_net_worth_usd: Decimal | None
    spot_value_usd: Decimal
    perp_equity_usd: Decimal
    defi_value_usd: Decimal
    cash_usd: Decimal
    debt_usd: Decimal
    realized_pnl_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    fee_expense_usd: Decimal | None
    funding_pnl_usd: Decimal | None
    all_time_pnl_usd: Decimal | None
    adjustment_usd: Decimal
    gross_long_usd: Decimal
    gross_short_usd: Decimal
    net_exposure_usd: Decimal
    margin_usage_percent: Decimal | None
    periods: list[DashboardPeriodRead]
    nav_history: list[PortfolioSnapshotRead]
    asset_allocation: list[DashboardAllocationRead]
    account_allocation: list[DashboardAllocationRead]
    chain_allocation: list[DashboardAllocationRead]
    product_allocation: list[DashboardAllocationRead]
    assets: list[DashboardAssetRead]
    accounts: list[DashboardAccountRead]
    positions: list[DashboardPositionRead]
    exposures: list[DashboardExposureRead]
    health: DashboardHealthRead
