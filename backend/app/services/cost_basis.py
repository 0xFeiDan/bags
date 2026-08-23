from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Asset,
    AssetPrice,
    AssetType,
    BalanceSnapshot,
    CostBasisOverride,
    CostBasisRun,
    CostLot,
    CostLotConsumption,
    CostMethod,
    CostOverrideType,
    EntryDirection,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    Portfolio,
    PositionCostSnapshot,
    RealizedPnlRecord,
    SyncRunStatus,
    TransferGroup,
    TransferGroupStatus,
    utc_now,
)
from app.schemas import CostBasisRunRequest
from app.services.security import as_utc

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")
# SQLite can round NUMERIC through a binary float on read. Treat sub-picounit
# residuals as zero so a fully funded transfer does not become a false
# insufficient-inventory event after persistence round-trips.
EPSILON = Decimal("0.000000000001")
STABLE_SYMBOLS = {"USD", "USDT", "USDC", "FDUSD", "DAI", "USDE", "USD1", "PYUSD", "TUSD"}
TRANSFER_SOURCE_TYPES = {LedgerEventType.WITHDRAW, LedgerEventType.TRANSFER_OUT}
TRANSFER_DESTINATION_TYPES = {LedgerEventType.DEPOSIT, LedgerEventType.TRANSFER_IN}


@dataclass
class ConsumedPiece:
    lot: CostLot
    quantity: Decimal
    cost_usd: Decimal | None


class CostBasisStats:
    def __init__(self) -> None:
        self.events_processed = 0
        self.lots_created = 0
        self.lot_consumptions = 0
        self.realized_records = 0
        self.transfer_groups_carried = 0
        self.position_snapshots = 0
        self.unknown_cost_lots = 0
        self.insufficient_inventory_events = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "events_processed": self.events_processed,
            "lots_created": self.lots_created,
            "lot_consumptions": self.lot_consumptions,
            "realized_records": self.realized_records,
            "transfer_groups_carried": self.transfer_groups_carried,
            "position_snapshots": self.position_snapshots,
            "unknown_cost_lots": self.unknown_cost_lots,
            "insufficient_inventory_events": self.insufficient_inventory_events,
        }


class CostBasisService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.run: CostBasisRun | None = None
        self.method = CostMethod.AVERAGE_COST
        self.stats = CostBasisStats()
        self.warnings: list[str] = []
        self.assets: dict[UUID, Asset] = {}
        self.entries_by_event: dict[UUID, list[LedgerEntry]] = defaultdict(list)
        self.transfer_by_source: dict[UUID, TransferGroup] = {}
        self.transfer_destinations: set[UUID] = set()

    def calculate(self, portfolio_id: UUID, request: CostBasisRunRequest) -> CostBasisRun:
        portfolio = self.session.get(Portfolio, portfolio_id)
        if not portfolio:
            raise ValueError("portfolio not found")
        if portfolio.base_currency.upper() != "USD":
            raise ValueError("Phase 1-7 cost basis supports USD portfolios only")
        try:
            method = request.method or CostMethod(portfolio.default_cost_method)
        except ValueError as error:
            raise ValueError("portfolio default cost method is invalid") from error
        as_of = request.as_of or datetime.now(timezone.utc)
        if as_utc(as_of) > datetime.now(timezone.utc):
            raise ValueError("as_of cannot be in the future")

        run = CostBasisRun(portfolio_id=portfolio_id, method=method, as_of=as_utc(as_of))
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        self.run = run
        self.method = method

        try:
            events = list(
                self.session.scalars(
                    select(LedgerEvent)
                    .where(
                        LedgerEvent.portfolio_id == portfolio_id,
                        LedgerEvent.status == EventStatus.POSTED,
                        LedgerEvent.occurred_at <= run.as_of,
                    )
                    .order_by(LedgerEvent.occurred_at.asc(), LedgerEvent.created_at.asc(), LedgerEvent.id.asc())
                )
            )
            self._load_context(portfolio_id, run.as_of, {event.id for event in events})
            for event in events:
                self._process_event(event)
                self.stats.events_processed += 1
            self._create_position_snapshots(portfolio_id, run.as_of)
            self.session.commit()
            self._finish(SyncRunStatus.PARTIAL if self.warnings else SyncRunStatus.SUCCEEDED)
        except Exception as error:
            self.session.rollback()
            self._finish(SyncRunStatus.FAILED, "COST_BASIS_ERROR", str(error)[:500])
        return self.session.get(CostBasisRun, run.id)  # type: ignore[return-value]

    def _load_context(self, portfolio_id: UUID, as_of: datetime, event_ids: set[UUID]) -> None:
        accounts = list(self.session.scalars(select(Account.id).where(Account.portfolio_id == portfolio_id)))
        entries = [] if not event_ids else list(self.session.scalars(select(LedgerEntry).where(LedgerEntry.ledger_event_id.in_(event_ids))))
        for entry in entries:
            self.entries_by_event[entry.ledger_event_id].append(entry)
        asset_ids = {entry.asset_id for entry in entries}
        if asset_ids:
            self.assets = {asset.id: asset for asset in self.session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))}
        if accounts:
            groups = list(
                self.session.scalars(
                    select(TransferGroup).where(
                        TransferGroup.portfolio_id == portfolio_id,
                        TransferGroup.status.in_([TransferGroupStatus.AUTO_MATCHED, TransferGroupStatus.CONFIRMED]),
                        TransferGroup.source_occurred_at <= as_of,
                        TransferGroup.source_event_id.in_(event_ids),
                    )
                )
            )
            self.transfer_by_source = {group.source_event_id: group for group in groups}
            self.transfer_destinations = {group.destination_event_id for group in groups}

    def _process_event(self, event: LedgerEvent) -> None:
        entries = self.entries_by_event.get(event.id, [])
        if event.id in self.transfer_destinations:
            return
        group = self.transfer_by_source.get(event.id)
        if group:
            self._process_internal_transfer(event, group)
            return
        if event.event_type == LedgerEventType.INTERNAL_TRANSFER:
            self._process_inline_internal_transfer(event, entries)
            return
        if self._is_derivative(event):
            self._process_derivative(event, entries)
            return
        if event.event_type == LedgerEventType.BUY:
            self._process_buy(event, entries)
        elif event.event_type in {LedgerEventType.SELL, LedgerEventType.SWAP, LedgerEventType.LIQUIDATION}:
            self._process_disposal(event, entries)
        elif event.event_type in TRANSFER_DESTINATION_TYPES:
            self._process_external_inflow(event, entries)
        elif event.event_type in TRANSFER_SOURCE_TYPES:
            self._process_external_outflow(event, entries)
        elif event.event_type == LedgerEventType.FEE:
            self._process_fee_event(event, entries)
        elif event.event_type in {LedgerEventType.AIRDROP, LedgerEventType.STAKING_REWARD, LedgerEventType.INTEREST}:
            self._process_income(event, entries)
        elif event.event_type == LedgerEventType.FUNDING:
            self._process_income(event, entries, category="income")
        elif event.event_type == LedgerEventType.REPAY:
            for entry in entries:
                if entry.direction == EntryDirection.DEBIT:
                    self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "repay", False)
        elif event.event_type == LedgerEventType.BORROW:
            for entry in entries:
                if entry.direction == EntryDirection.CREDIT:
                    self._create_lot(entry, event, None, "borrow", cost_known=False)
            self._warn_once(f"Borrow event {event.id} needs a liability engine; received quantity has unknown cost.")
        elif event.event_type == LedgerEventType.MANUAL_ADJUSTMENT:
            self._process_manual_adjustment(event, entries)

    def _process_buy(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        credits = [entry for entry in entries if entry.direction == EntryDirection.CREDIT and not entry.fee_flag]
        debits = [entry for entry in entries if entry.direction == EntryDirection.DEBIT and not entry.fee_flag]
        fees = [entry for entry in entries if entry.direction == EntryDirection.DEBIT and entry.fee_flag]
        if not credits:
            self._warn_once(f"Buy event {event.id} has no acquired asset entry.")
            return

        acquired_ids = {entry.asset_id for entry in credits}
        consideration = self._sum_entry_values(debits, event.occurred_at)
        external_fees = [entry for entry in fees if entry.asset_id not in acquired_ids]
        fee_value = self._sum_entry_values(external_fees, event.occurred_at)
        total_cost = None if consideration is None or fee_value is None else consideration + fee_value

        for entry in debits:
            self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "purchase_consideration", False)
        for entry in external_fees:
            self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "capitalized_fee", False)

        weights = self._credit_weights(credits, event.occurred_at)
        for index, entry in enumerate(credits):
            same_asset_fee = sum((fee.quantity for fee in fees if fee.asset_id == entry.asset_id and fee.account_id == entry.account_id), ZERO)
            net_quantity = entry.quantity - same_asset_fee
            if net_quantity <= ZERO:
                self._warn_once(f"Buy event {event.id} has a fee greater than or equal to acquired quantity.")
                continue
            override = self._event_override(event, entry)
            entry_cost = override
            if entry_cost is None and total_cost is not None:
                entry_cost = total_cost * weights[index]
            if entry_cost is None and entry.unit_price_usd is not None:
                entry_cost = net_quantity * entry.unit_price_usd
            self._create_lot(entry, event, entry_cost, "buy", quantity=net_quantity, cost_known=entry_cost is not None)

    def _process_disposal(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        debits = [entry for entry in entries if entry.direction == EntryDirection.DEBIT and not entry.fee_flag]
        credits = [entry for entry in entries if entry.direction == EntryDirection.CREDIT and not entry.fee_flag]
        fees = [entry for entry in entries if entry.direction == EntryDirection.DEBIT and entry.fee_flag]
        gross_proceeds = self._sum_entry_values(credits, event.occurred_at)
        if gross_proceeds is None:
            gross_proceeds = self._sum_entry_values(debits, event.occurred_at)
        fee_value = self._sum_entry_values(fees, event.occurred_at)
        net_proceeds = None if gross_proceeds is None or fee_value is None else max(gross_proceeds - fee_value, ZERO)

        disposal_weights = self._entry_weights(debits, event.occurred_at)
        for index, entry in enumerate(debits):
            pieces = self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, event.event_type.value, True)
            cost = self._known_cost(pieces)
            proceeds = None if net_proceeds is None else net_proceeds * disposal_weights[index]
            allocated_fee = ZERO if fee_value is None else fee_value * disposal_weights[index]
            self._add_pnl(
                event,
                entry.account_id,
                entry.asset_id,
                "spot",
                entry.quantity,
                proceeds,
                cost,
                allocated_fee,
                None if proceeds is None or cost is None else proceeds - cost,
            )

        credited_ids = {entry.asset_id for entry in credits}
        for fee in fees:
            if fee.asset_id not in credited_ids:
                self._consume(fee.account_id, fee.asset_id, fee.quantity, event, fee, "trading_fee", False)
        credit_weights = self._credit_weights(credits, event.occurred_at)
        for index, entry in enumerate(credits):
            same_asset_fee = sum((fee.quantity for fee in fees if fee.asset_id == entry.asset_id and fee.account_id == entry.account_id), ZERO)
            net_quantity = entry.quantity - same_asset_fee
            if net_quantity <= ZERO:
                continue
            acquired_cost = None if net_proceeds is None else net_proceeds * credit_weights[index]
            self._create_lot(entry, event, acquired_cost, "swap_receive" if event.event_type == LedgerEventType.SWAP else "sale_proceeds", quantity=net_quantity, cost_known=acquired_cost is not None)

    def _process_internal_transfer(self, event: LedgerEvent, group: TransferGroup) -> None:
        source_entry = next(
            (
                entry
                for entry in self.entries_by_event.get(event.id, [])
                if not entry.fee_flag
                and entry.direction == EntryDirection.DEBIT
                and entry.account_id == group.source_account_id
                and entry.asset_id == group.source_asset_id
            ),
            None,
        )
        destination_entry = next(
            (
                entry
                for entry in self.entries_by_event.get(group.destination_event_id, [])
                if not entry.fee_flag
                and entry.direction == EntryDirection.CREDIT
                and entry.account_id == group.destination_account_id
                and entry.asset_id == group.destination_asset_id
            ),
            None,
        )
        if not source_entry or not destination_entry:
            self._warn_once(f"Transfer Group {group.reference} is missing a source or destination ledger entry.")
            return
        pieces = self._consume(
            group.source_account_id,
            group.source_asset_id,
            group.source_amount,
            event,
            source_entry,
            "internal_transfer",
            False,
            group.id,
        )
        consumed_quantity = sum((piece.quantity for piece in pieces), ZERO)
        all_known = consumed_quantity + EPSILON >= group.source_amount and all(piece.cost_usd is not None for piece in pieces)
        scale = group.destination_amount / group.source_amount if group.source_amount > ZERO else ZERO
        carried_total = ZERO
        created_quantity = ZERO
        source_fully_consumed = consumed_quantity + EPSILON >= group.source_amount
        for index, piece in enumerate(pieces):
            destination_quantity = piece.quantity * scale
            if source_fully_consumed and index == len(pieces) - 1:
                destination_quantity = group.destination_amount - created_quantity
            destination_quantity = max(destination_quantity, ZERO)
            if destination_quantity <= EPSILON:
                continue
            destination_cost = None if piece.cost_usd is None else piece.cost_usd * scale
            self._create_lot(
                destination_entry,
                self.session.get(LedgerEvent, group.destination_event_id),  # type: ignore[arg-type]
                destination_cost,
                "internal_transfer",
                quantity=destination_quantity,
                cost_known=destination_cost is not None,
                parent_lot_id=piece.lot.id,
                transfer_group_id=group.id,
                acquired_at=group.destination_occurred_at,
            )
            created_quantity += destination_quantity
            if destination_cost is not None:
                carried_total += destination_cost
        if created_quantity + EPSILON < group.destination_amount:
            missing = group.destination_amount - created_quantity
            self._create_lot(
                destination_entry,
                self.session.get(LedgerEvent, group.destination_event_id),  # type: ignore[arg-type]
                None,
                "internal_transfer_missing_source",
                quantity=missing,
                cost_known=False,
                transfer_group_id=group.id,
                acquired_at=group.destination_occurred_at,
            )
            all_known = False
        group.original_cost_basis = carried_total if all_known else None
        self.stats.transfer_groups_carried += 1

        source_cost = self._known_cost(pieces)
        fee_cost = None if source_cost is None else max(source_cost - carried_total, ZERO)
        # Exchanges use two valid withdrawal conventions:
        #   (1) source_amount already includes the same-asset network fee
        #       (1.0000 sent, 0.9998 received, 0.0002 fee), and
        #   (2) the fee is an additional debit (1.0000 sent, plus 0.0002 fee).
        # In convention (1) the source-to-destination haircut has already
        # consumed the fee's cost. Consuming the fee ledger entry again would
        # double-debit inventory. In convention (2), consume that fee entry so
        # it cannot leave a ghost asset balance.
        fee_is_embedded_in_source_amount = (
            group.fee_amount > ZERO
            and group.fee_asset_id == group.source_asset_id
            and abs((group.source_amount - group.destination_amount) - group.fee_amount) <= EPSILON
        )
        if group.fee_amount > ZERO and group.fee_asset_id and not fee_is_embedded_in_source_amount:
            fee_entry = next(
                (
                    entry
                    for entry in self.entries_by_event.get(event.id, [])
                    if entry.fee_flag and entry.asset_id == group.fee_asset_id and entry.direction == EntryDirection.DEBIT
                ),
                None,
            )
            fee_pieces = self._consume(
                group.source_account_id,
                group.fee_asset_id,
                group.fee_amount,
                event,
                fee_entry,
                "transfer_fee",
                False,
                group.id,
            )
            fee_cost = self._known_cost(fee_pieces)
        if group.fee_amount > ZERO:
            self._add_pnl(event, group.source_account_id, group.fee_asset_id or group.source_asset_id, "fee", group.fee_amount, ZERO, fee_cost, fee_cost or ZERO, None if fee_cost is None else -fee_cost)
        # Internal transfers intentionally never create a spot realized-PnL record.

    def _process_inline_internal_transfer(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        debits = [entry for entry in entries if entry.direction == EntryDirection.DEBIT and not entry.fee_flag]
        credits = [entry for entry in entries if entry.direction == EntryDirection.CREDIT and not entry.fee_flag]
        if len(debits) != 1 or len(credits) != 1:
            if event.metadata_json.get("internal_account_transfer"):
                return
            self._warn_once(f"Internal transfer event {event.id} must contain one source and one destination leg.")
            return
        source, destination = debits[0], credits[0]
        pieces = self._consume(source.account_id, source.asset_id, source.quantity, event, source, "internal_transfer", False)
        consumed_quantity = sum((piece.quantity for piece in pieces), ZERO)
        scale = destination.quantity / source.quantity if source.quantity > ZERO else ZERO
        created_quantity = ZERO
        source_fully_consumed = consumed_quantity + EPSILON >= source.quantity
        for index, piece in enumerate(pieces):
            destination_quantity = piece.quantity * scale
            if source_fully_consumed and index == len(pieces) - 1:
                destination_quantity = destination.quantity - created_quantity
            destination_cost = None if piece.cost_usd is None else piece.cost_usd * scale
            if destination_quantity > EPSILON:
                self._create_lot(
                    destination,
                    event,
                    destination_cost,
                    "internal_transfer",
                    quantity=destination_quantity,
                    cost_known=destination_cost is not None,
                    parent_lot_id=piece.lot.id,
                )
                created_quantity += destination_quantity
        if created_quantity + EPSILON < destination.quantity:
            self._create_lot(
                destination,
                event,
                None,
                "internal_transfer_missing_source",
                quantity=destination.quantity - created_quantity,
                cost_known=False,
            )
        for fee in (entry for entry in entries if entry.direction == EntryDirection.DEBIT and entry.fee_flag):
            fee_pieces = self._consume(fee.account_id, fee.asset_id, fee.quantity, event, fee, "transfer_fee", False)
            fee_cost = self._known_cost(fee_pieces)
            self._add_pnl(event, fee.account_id, fee.asset_id, "fee", fee.quantity, ZERO, fee_cost, fee_cost or ZERO, None if fee_cost is None else -fee_cost)

    def _process_external_inflow(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        for entry in entries:
            if entry.direction != EntryDirection.CREDIT or entry.fee_flag:
                continue
            override = self._event_override(event, entry)
            value = override if override is not None else self._entry_value(entry, event.occurred_at)
            self._create_lot(entry, event, value, "external_inflow", cost_known=value is not None)
            if value is None:
                self._warn_once(f"Unknown deposit {event.id} requires classification or a custom event cost override.")

    def _process_external_outflow(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        for entry in entries:
            if entry.direction == EntryDirection.DEBIT and not entry.fee_flag:
                self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "unmatched_withdrawal", False)
            elif entry.direction == EntryDirection.DEBIT and entry.fee_flag:
                pieces = self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "withdrawal_fee", False)
                fee_cost = self._known_cost(pieces)
                self._add_pnl(event, entry.account_id, entry.asset_id, "fee", entry.quantity, ZERO, fee_cost, fee_cost or ZERO, None if fee_cost is None else -fee_cost)
        self._warn_once(f"Unmatched withdrawal {event.id} removed inventory without realized PnL; classify it before final reporting.")

    def _process_fee_event(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        for entry in entries:
            if entry.direction != EntryDirection.DEBIT:
                continue
            pieces = self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "fee", False)
            cost = self._known_cost(pieces)
            value = self._entry_value(entry, event.occurred_at) or cost
            self._add_pnl(event, entry.account_id, entry.asset_id, "fee", entry.quantity, ZERO, cost, value or ZERO, -(value or ZERO))

    def _process_income(self, event: LedgerEvent, entries: list[LedgerEntry], category: str = "income") -> None:
        for entry in entries:
            value = self._entry_value(entry, event.occurred_at)
            if entry.direction == EntryDirection.CREDIT:
                if event.event_type == LedgerEventType.AIRDROP and value is None:
                    value = ZERO
                override = self._event_override(event, entry)
                cost = override if override is not None else value
                self._create_lot(entry, event, cost, event.event_type.value, cost_known=cost is not None)
                self._add_pnl(event, entry.account_id, entry.asset_id, category, entry.quantity, value, ZERO, ZERO, value)
            else:
                pieces = self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, event.event_type.value, False)
                expense = value or self._known_cost(pieces)
                self._add_pnl(event, entry.account_id, entry.asset_id, category, entry.quantity, ZERO, self._known_cost(pieces), ZERO, None if expense is None else -expense)

    def _process_manual_adjustment(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        for entry in entries:
            if entry.direction == EntryDirection.CREDIT:
                cost = self._event_override(event, entry)
                if cost is None:
                    cost = self._entry_value(entry, event.occurred_at)
                self._create_lot(entry, event, cost, "manual_adjustment", cost_known=cost is not None)
            else:
                self._consume(entry.account_id, entry.asset_id, entry.quantity, event, entry, "manual_adjustment", False)

    def _process_derivative(self, event: LedgerEvent, entries: list[LedgerEntry]) -> None:
        values: list[tuple[LedgerEntry, Decimal | None]] = [(entry, self._entry_value(entry, event.occurred_at)) for entry in entries]
        if any(value is None for _, value in values):
            self._warn_once(f"Derivative event {event.id} is missing USD valuation and was kept separate from Spot Cost Basis.")
            representative = next((entry for entry, _ in values if not entry.fee_flag), values[0][0] if values else None)
            if representative:
                self._add_pnl(event, representative.account_id, representative.asset_id, "derivative", None, None, None, ZERO, None, {"spot_cost_basis_excluded": True, "valuation_incomplete": True})
            return
        credits = sum((value or ZERO for entry, value in values if entry.direction == EntryDirection.CREDIT), ZERO)
        debits = sum((value or ZERO for entry, value in values if entry.direction == EntryDirection.DEBIT), ZERO)
        fees = sum((value or ZERO for entry, value in values if entry.fee_flag), ZERO)
        representative = next((entry for entry, _ in values if not entry.fee_flag), values[0][0] if values else None)
        if representative:
            self._add_pnl(event, representative.account_id, representative.asset_id, "derivative", None, credits, debits - fees, fees, credits - debits, {"spot_cost_basis_excluded": True})

    def _create_lot(
        self,
        entry: LedgerEntry,
        event: LedgerEvent,
        cost_usd: Decimal | None,
        acquisition_type: str,
        *,
        quantity: Decimal | None = None,
        cost_known: bool,
        parent_lot_id: UUID | None = None,
        transfer_group_id: UUID | None = None,
        acquired_at: datetime | None = None,
    ) -> CostLot:
        assert self.run is not None
        actual_quantity = quantity if quantity is not None else entry.quantity
        lot = CostLot(
            run_id=self.run.id,
            portfolio_id=self.run.portfolio_id,
            account_id=entry.account_id,
            asset_id=entry.asset_id,
            origin_event_id=event.id,
            origin_entry_id=entry.id,
            parent_lot_id=parent_lot_id,
            transfer_group_id=transfer_group_id,
            acquired_at=acquired_at or event.occurred_at,
            original_quantity=actual_quantity,
            remaining_quantity=actual_quantity,
            original_cost_usd=cost_usd if cost_known else None,
            remaining_cost_usd=cost_usd if cost_known else None,
            cost_known=cost_known,
            acquisition_type=acquisition_type,
            metadata_json={},
        )
        self.session.add(lot)
        self.session.flush()
        self.stats.lots_created += 1
        if not cost_known:
            self.stats.unknown_cost_lots += 1
            self._warn_once(f"Cost Lot created from event {event.id} has unknown cost basis.")
        return lot

    def _consume(
        self,
        account_id: UUID,
        asset_id: UUID,
        quantity: Decimal,
        event: LedgerEvent,
        entry: LedgerEntry | None,
        disposition_type: str,
        realizes_pnl: bool,
        transfer_group_id: UUID | None = None,
    ) -> list[ConsumedPiece]:
        assert self.run is not None
        lots = list(
            self.session.scalars(
                select(CostLot).where(
                    CostLot.run_id == self.run.id,
                    CostLot.account_id == account_id,
                    CostLot.asset_id == asset_id,
                    CostLot.remaining_quantity > ZERO,
                    CostLot.acquired_at <= event.occurred_at,
                )
            )
        )
        lots.sort(key=lambda lot: (as_utc(lot.acquired_at), str(lot.id)), reverse=self.method == CostMethod.LIFO)
        if self.method == CostMethod.AVERAGE_COST:
            pieces = self._consume_average(lots, quantity)
        else:
            pieces = self._consume_ordered(lots, quantity)
        for piece in pieces:
            self.session.add(
                CostLotConsumption(
                    run_id=self.run.id,
                    lot_id=piece.lot.id,
                    ledger_event_id=event.id,
                    ledger_entry_id=entry.id if entry else None,
                    transfer_group_id=transfer_group_id,
                    quantity=piece.quantity,
                    cost_basis_usd=piece.cost_usd,
                    disposition_type=disposition_type,
                    realizes_pnl=realizes_pnl,
                    occurred_at=event.occurred_at,
                )
            )
            self.stats.lot_consumptions += 1
        consumed = sum((piece.quantity for piece in pieces), ZERO)
        if consumed + EPSILON < quantity:
            self.stats.insufficient_inventory_events += 1
            self._warn_once(f"Event {event.id} disposes {quantity} but only {consumed} is available for asset {asset_id} in account {account_id}.")
        return pieces

    def _consume_ordered(self, lots: list[CostLot], quantity: Decimal) -> list[ConsumedPiece]:
        remaining = quantity
        pieces: list[ConsumedPiece] = []
        for lot in lots:
            if remaining <= EPSILON:
                break
            before_quantity = lot.remaining_quantity
            take = min(before_quantity, remaining)
            ratio = take / before_quantity
            cost = None if not lot.cost_known or lot.remaining_cost_usd is None else lot.remaining_cost_usd * ratio
            lot.remaining_quantity = max(before_quantity - take, ZERO)
            if lot.cost_known and lot.remaining_cost_usd is not None and cost is not None:
                lot.remaining_cost_usd = max(lot.remaining_cost_usd - cost, ZERO)
            pieces.append(ConsumedPiece(lot, take, cost))
            remaining -= take
        return pieces

    def _consume_average(self, lots: list[CostLot], quantity: Decimal) -> list[ConsumedPiece]:
        total_quantity = sum((lot.remaining_quantity for lot in lots), ZERO)
        target = min(quantity, total_quantity)
        if target <= EPSILON:
            return []
        ratio = target / total_quantity
        pieces: list[ConsumedPiece] = []
        allocated = ZERO
        for index, lot in enumerate(lots):
            before_quantity = lot.remaining_quantity
            take = target - allocated if index == len(lots) - 1 else before_quantity * ratio
            take = min(max(take, ZERO), before_quantity)
            cost = None if not lot.cost_known or lot.remaining_cost_usd is None else lot.remaining_cost_usd * (take / before_quantity)
            lot.remaining_quantity = max(before_quantity - take, ZERO)
            if lot.cost_known and lot.remaining_cost_usd is not None and cost is not None:
                lot.remaining_cost_usd = max(lot.remaining_cost_usd - cost, ZERO)
            if take > EPSILON:
                pieces.append(ConsumedPiece(lot, take, cost))
                allocated += take
        return pieces

    def _create_position_snapshots(self, portfolio_id: UUID, as_of: datetime) -> None:
        assert self.run is not None
        lots = list(
            self.session.scalars(
                select(CostLot).where(
                    CostLot.run_id == self.run.id,
                    CostLot.remaining_quantity > EPSILON,
                    CostLot.acquired_at <= as_of,
                )
            )
        )
        grouped: dict[tuple[UUID, UUID], list[CostLot]] = defaultdict(list)
        for lot in lots:
            grouped[(lot.account_id, lot.asset_id)].append(lot)
        balances, snapshot_accounts = self._latest_balances(portfolio_id, as_of)
        for key in set(grouped) | set(balances):
            account_id, asset_id = key
            asset_lots = grouped.get(key, [])
            ledger_quantity = sum((lot.remaining_quantity for lot in asset_lots), ZERO)
            ledger_cost = None if any(not lot.cost_known or lot.remaining_cost_usd is None for lot in asset_lots) else sum((lot.remaining_cost_usd or ZERO for lot in asset_lots), ZERO)
            # A balance snapshot is a complete account-state observation, so
            # an asset absent from that observation is zero rather than an
            # invitation to resurrect an old CostLot. Accounts without any
            # snapshot continue to use ledger-derived quantities.
            quantity = balances.get(key, ZERO) if account_id in snapshot_accounts else ledger_quantity
            if quantity <= EPSILON:
                continue
            # Account snapshots are the quantity authority.  Cost can be carried
            # only when the ledger fully explains the observed quantity; otherwise
            # expose the value but leave cost/PnL incomplete instead of inventing it.
            calculated = ledger_cost if quantity == ledger_quantity else (
                ledger_cost * quantity / ledger_quantity
                if ledger_cost is not None and ledger_quantity > ZERO and quantity < ledger_quantity
                else None
            )
            manual = self._position_override(portfolio_id, account_id, asset_id, as_of)
            effective = manual if manual is not None else calculated
            asset = self.assets.get(asset_id) or self.session.get(Asset, asset_id)
            price = Decimal("1") if asset and asset.asset_type == AssetType.FIAT and asset.canonical_symbol == "USD" else self._price(asset_id, as_of)
            market_value = None if price is None else quantity * price
            unrealized = None if market_value is None or effective is None else market_value - effective
            unrealized_percent = None if unrealized is None or effective is None or effective == ZERO else unrealized / effective * ONE_HUNDRED
            self.session.add(
                PositionCostSnapshot(
                    run_id=self.run.id,
                    portfolio_id=portfolio_id,
                    account_id=account_id,
                    asset_id=asset_id,
                    quantity=quantity,
                    calculated_cost_usd=calculated,
                    manual_cost_usd=manual,
                    effective_cost_usd=effective,
                    average_unit_cost_usd=None if effective is None or quantity == ZERO else effective / quantity,
                    market_price_usd=price,
                    market_value_usd=market_value,
                    unrealized_pnl_usd=unrealized,
                    unrealized_pnl_percent=unrealized_percent,
                    as_of=as_of,
                )
            )
            self.stats.position_snapshots += 1

    def _latest_balances(self, portfolio_id: UUID, as_of: datetime) -> tuple[dict[tuple[UUID, UUID], Decimal], set[UUID]]:
        account_ids = list(self.session.scalars(select(Account.id).where(Account.portfolio_id == portfolio_id)))
        result: dict[tuple[UUID, UUID], Decimal] = {}
        snapshot_accounts: set[UUID] = set()
        for account_id in account_ids:
            latest_at = self.session.scalar(
                select(BalanceSnapshot.as_of)
                .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.as_of <= as_of)
                .order_by(BalanceSnapshot.as_of.desc())
                .limit(1)
            )
            if not latest_at:
                continue
            snapshot_accounts.add(account_id)
            for row in self.session.scalars(
                select(BalanceSnapshot).where(
                    BalanceSnapshot.account_id == account_id,
                    BalanceSnapshot.as_of == latest_at,
                )
            ):
                result[(account_id, row.asset_id)] = row.quantity
        return result, snapshot_accounts

    def _event_override(self, event: LedgerEvent, entry: LedgerEntry) -> Decimal | None:
        return self.session.scalar(
            select(CostBasisOverride.total_cost_usd)
            .where(
                CostBasisOverride.portfolio_id == event.portfolio_id,
                CostBasisOverride.asset_id == entry.asset_id,
                CostBasisOverride.ledger_event_id == event.id,
                CostBasisOverride.override_type == CostOverrideType.EVENT_TOTAL,
                (CostBasisOverride.account_id.is_(None)) | (CostBasisOverride.account_id == entry.account_id),
            )
            .order_by(CostBasisOverride.created_at.desc(), CostBasisOverride.id.desc())
            .limit(1)
        )

    def _position_override(self, portfolio_id: UUID, account_id: UUID, asset_id: UUID, as_of: datetime) -> Decimal | None:
        return self.session.scalar(
            select(CostBasisOverride.total_cost_usd)
            .where(
                CostBasisOverride.portfolio_id == portfolio_id,
                CostBasisOverride.asset_id == asset_id,
                CostBasisOverride.account_id == account_id,
                CostBasisOverride.ledger_event_id.is_(None),
                CostBasisOverride.override_type == CostOverrideType.POSITION_TOTAL,
                CostBasisOverride.created_at <= as_of,
            )
            .order_by(CostBasisOverride.created_at.desc(), CostBasisOverride.id.desc())
            .limit(1)
        )

    def _entry_value(self, entry: LedgerEntry, at: datetime) -> Decimal | None:
        if entry.unit_price_usd is not None:
            return entry.quantity * entry.unit_price_usd
        asset = self.assets.get(entry.asset_id) or self.session.get(Asset, entry.asset_id)
        if asset and asset.asset_type == AssetType.FIAT and asset.canonical_symbol == "USD":
            return entry.quantity
        price = self._price(entry.asset_id, at)
        return None if price is None else entry.quantity * price

    def _price(self, asset_id: UUID, at: datetime) -> Decimal | None:
        row = self.session.scalar(
            select(AssetPrice)
            .where(AssetPrice.asset_id == asset_id, AssetPrice.as_of <= at)
            .order_by(AssetPrice.as_of.desc(), AssetPrice.created_at.desc())
            .limit(1)
        )
        # Cost-basis reconstruction is historical accounting: a price that was
        # valid at the event/valuation time remains valid even though it is old
        # today.  Freshness is enforced by DashboardService when it produces a
        # current NAV, rather than making past lots and realized PnL disappear.
        return row.price_usd if row else None

    def _sum_entry_values(self, entries: list[LedgerEntry], at: datetime) -> Decimal | None:
        if not entries:
            return ZERO
        values = [self._entry_value(entry, at) for entry in entries]
        return None if any(value is None for value in values) else sum((value or ZERO for value in values), ZERO)

    def _entry_weights(self, entries: list[LedgerEntry], at: datetime) -> list[Decimal]:
        if not entries:
            return []
        values = [self._entry_value(entry, at) for entry in entries]
        if all(value is not None for value in values) and sum((value or ZERO for value in values), ZERO) > ZERO:
            total = sum((value or ZERO for value in values), ZERO)
            return [(value or ZERO) / total for value in values]
        total_quantity = sum((entry.quantity for entry in entries), ZERO)
        return [entry.quantity / total_quantity for entry in entries]

    def _credit_weights(self, entries: list[LedgerEntry], at: datetime) -> list[Decimal]:
        return self._entry_weights(entries, at)

    @staticmethod
    def _known_cost(pieces: list[ConsumedPiece]) -> Decimal | None:
        if not pieces or any(piece.cost_usd is None for piece in pieces):
            return None
        return sum((piece.cost_usd or ZERO for piece in pieces), ZERO)

    def _add_pnl(
        self,
        event: LedgerEvent,
        account_id: UUID,
        asset_id: UUID,
        category: str,
        quantity: Decimal | None,
        proceeds: Decimal | None,
        cost: Decimal | None,
        fee: Decimal,
        pnl: Decimal | None,
        metadata: dict | None = None,
    ) -> None:
        assert self.run is not None
        self.session.add(
            RealizedPnlRecord(
                run_id=self.run.id,
                ledger_event_id=event.id,
                account_id=account_id,
                asset_id=asset_id,
                category=category,
                quantity=quantity,
                proceeds_usd=proceeds,
                cost_basis_usd=cost,
                fee_usd=fee,
                realized_pnl_usd=pnl,
                occurred_at=event.occurred_at,
                metadata_json=metadata or {},
            )
        )
        self.stats.realized_records += 1

    @staticmethod
    def _is_derivative(event: LedgerEvent) -> bool:
        metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        if metadata.get("internal_account_transfer"):
            return False
        market = str(metadata.get("market_type", "")).lower()
        return bool(metadata.get("derivative_trade")) or "perp" in market or market in {"usdm", "coinm"}

    def _warn_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _finish(self, status: SyncRunStatus, error_code: str | None = None, error_message: str | None = None) -> None:
        assert self.run is not None
        run = self.session.get(CostBasisRun, self.run.id)
        if not run:
            return
        run.status = status
        run.stats_json = self.stats.as_dict()
        run.warnings_json = self.warnings
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = utc_now()
        self.session.commit()
