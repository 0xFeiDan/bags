from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountEquitySnapshot,
    AccountKind,
    Asset,
    AssetPrice,
    AssetType,
    BalanceSnapshot,
    CostBasisOverride,
    CostBasisRun,
    CostLot,
    CostOverrideType,
    EntryDirection,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    PnlAdjustment,
    Portfolio,
    PortfolioSnapshot,
    PositionCostSnapshot,
    PositionSnapshot,
    RawEvent,
    RealizedPnlRecord,
    SyncCursor,
    SyncRunStatus,
    TransferCandidate,
    TransferCandidateStatus,
    TransferGroup,
    TransferGroupStatus,
)
from app.core.config import get_settings
from app.schemas import (
    CostBasisRunRequest,
    DashboardAccountRead,
    DashboardAllocationRead,
    DashboardAssetRead,
    DashboardBackfillRead,
    DashboardExposureRead,
    DashboardHealthRead,
    DashboardPeriodRead,
    DashboardPositionRead,
    DashboardSummaryRead,
)
from app.services.cost_basis import CostBasisService, STABLE_SYMBOLS
from app.services.security import as_utc

ZERO = Decimal("0")
HUNDRED = Decimal("100")
MONEY_QUANTUM = Decimal("0.000000000000000001")
EXTERNAL_IN_TYPES = {LedgerEventType.DEPOSIT, LedgerEventType.TRANSFER_IN}
EXTERNAL_OUT_TYPES = {LedgerEventType.WITHDRAW, LedgerEventType.TRANSFER_OUT}
ACTIVE_TRANSFER_STATUSES = {TransferGroupStatus.AUTO_MATCHED, TransferGroupStatus.CONFIRMED}


def money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 60
        return value.quantize(MONEY_QUANTUM)


class DashboardService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._price_cache: dict[tuple[UUID, datetime], Decimal | None] = {}
        self._max_valuation_age = timedelta(hours=max(0, get_settings().valuation_max_age_hours))

    def summary(self, portfolio_id: UUID, run_id: UUID | None = None, as_of: datetime | None = None) -> DashboardSummaryRead:
        portfolio = self.session.get(Portfolio, portfolio_id)
        if not portfolio:
            raise ValueError("portfolio not found")
        if portfolio.base_currency.upper() != "USD":
            raise ValueError("Phase 1-7 dashboard supports USD portfolios only")
        run = self._cost_run(portfolio_id, run_id, as_of)
        current_at = as_utc(run.as_of)
        accounts = list(self.session.scalars(select(Account).where(Account.portfolio_id == portfolio_id)))
        account_map = {account.id: account for account in accounts}
        cost_positions = list(self.session.scalars(select(PositionCostSnapshot).where(PositionCostSnapshot.run_id == run.id)))
        records = list(self.session.scalars(select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id)))
        event_ids = {record.ledger_event_id for record in records}
        events = {
            event.id: event
            for event in self.session.scalars(select(LedgerEvent).where(LedgerEvent.id.in_(event_ids)))
        } if event_ids else {}
        assets = {
            asset.id: asset
            for asset in self.session.scalars(select(Asset).where(Asset.id.in_({row.asset_id for row in cost_positions})))
        } if cost_positions else {}

        latest_positions = self._latest_positions(accounts, current_at)
        equity_by_account = self._latest_equities(accounts, current_at)
        derivative_values, derivative_complete = self._derivative_account_values(
            accounts, latest_positions, equity_by_account, current_at
        )
        skip_cost_accounts = {
            account.id
            for account in accounts
            if self._is_binance_derivative(account) and account.id in derivative_values
        }

        warnings = list(run.warnings_json or [])
        account_spot: dict[UUID, Decimal] = defaultdict(lambda: ZERO)
        account_cash: dict[UUID, Decimal] = defaultdict(lambda: ZERO)
        account_complete: dict[UUID, bool] = {account.id: True for account in accounts}
        asset_rows: dict[UUID, list[PositionCostSnapshot]] = defaultdict(list)
        spot_value = ZERO
        cash_value = ZERO
        defi_value = ZERO
        valuation_complete = True

        for row in cost_positions:
            if row.account_id in skip_cost_accounts:
                continue
            asset_rows[row.asset_id].append(row)
            account = account_map.get(row.account_id)
            asset = assets.get(row.asset_id) or self.session.get(Asset, row.asset_id)
            # PositionCostSnapshot can be built for a historical reporting
            # point. Revalue it here with a price that is both available and
            # fresh relative to the dashboard target, so stale lot prices can
            # never silently become a current NAV.
            current_price = self._asset_price(asset, current_at) if asset else None
            if current_price is None:
                valuation_complete = False
                account_complete[row.account_id] = False
                warnings.append(f"Asset {asset.canonical_symbol if asset else row.asset_id} is missing a current USD price.")
                continue
            value = row.quantity * current_price
            if account and account.kind == AccountKind.DEFI:
                defi_value += value
                account_spot[row.account_id] += value
            elif asset and self._is_cash(asset):
                cash_value += value
                account_cash[row.account_id] += value
            else:
                spot_value += value
                account_spot[row.account_id] += value

        stale_balance_accounts = self._stale_balance_accounts(accounts, current_at, skip_cost_accounts)
        if stale_balance_accounts:
            valuation_complete = False
            for account in stale_balance_accounts:
                account_complete[account.id] = False
            warnings.append("One or more account balance snapshots are stale; current NAV was not calculated.")

        perp_equity = sum(derivative_values.values(), ZERO)
        if not all(derivative_complete.values()):
            valuation_complete = False
            warnings.append("One or more derivative accounts are missing a complete USD equity valuation.")

        borrow_count = self.session.scalar(
            select(LedgerEvent.id)
            .where(
                LedgerEvent.portfolio_id == portfolio_id,
                LedgerEvent.status == EventStatus.POSTED,
                LedgerEvent.event_type == LedgerEventType.BORROW,
                LedgerEvent.occurred_at <= current_at,
            )
            .limit(1)
        )
        debt = ZERO
        if borrow_count:
            valuation_complete = False
            warnings.append("Borrow events exist, but the debt/liability engine is not available in Phase 7.")

        total_nav = None if not valuation_complete else spot_value + cash_value + defi_value + perp_equity - debt
        adjustments = list(
            self.session.scalars(
                select(PnlAdjustment).where(
                    PnlAdjustment.portfolio_id == portfolio_id,
                    PnlAdjustment.occurred_at <= current_at,
                )
            )
        )
        adjustment_total = sum((row.amount_usd for row in adjustments), ZERO)
        incomplete_pnl = sum(1 for record in records if record.realized_pnl_usd is None)
        system_realized = None if incomplete_pnl else sum((record.realized_pnl_usd or ZERO for record in records), ZERO)
        realized = None if system_realized is None else system_realized + adjustment_total
        fee_expense = None if any(record.fee_usd is None for record in records) else sum((record.fee_usd for record in records), ZERO)
        funding_records = [record for record in records if events.get(record.ledger_event_id) and events[record.ledger_event_id].event_type == LedgerEventType.FUNDING]
        funding_pnl = None if any(record.realized_pnl_usd is None for record in funding_records) else sum((record.realized_pnl_usd or ZERO for record in funding_records), ZERO)
        spot_unrealized = None if any(row.unrealized_pnl_usd is None for row in cost_positions if row.account_id not in skip_cost_accounts) else sum(
            (row.unrealized_pnl_usd or ZERO for row in cost_positions if row.account_id not in skip_cost_accounts), ZERO
        )
        perp_unrealized, perp_unrealized_complete = self._perp_unrealized(latest_positions, equity_by_account, current_at)
        unrealized = None if spot_unrealized is None or not perp_unrealized_complete else spot_unrealized + perp_unrealized
        all_time_pnl = None if realized is None or unrealized is None else realized + unrealized

        position_views = self._position_views(latest_positions, account_map)
        exposure_views = self._exposures(cost_positions, latest_positions, assets, skip_cost_accounts)
        gross_long = sum((row.gross_long_usd for row in exposure_views), ZERO)
        gross_short = sum((row.gross_short_usd for row in exposure_views), ZERO)
        margin_used = self._margin_used(equity_by_account, position_views)
        margin_base = sum((max(value, ZERO) for value in derivative_values.values()), ZERO)
        margin_usage = None if margin_base <= ZERO else margin_used / margin_base * HUNDRED

        assets_view = self._asset_views(run, asset_rows, assets, records)
        accounts_view = self._account_views(
            accounts,
            account_spot,
            account_cash,
            derivative_values,
            derivative_complete,
            account_complete,
            equity_by_account,
            records,
            events,
            adjustments,
            latest_positions,
            self._spot_unrealized_by_account(cost_positions, skip_cost_accounts),
            current_at,
        )
        denominator = total_nav if total_nav is not None and total_nav > ZERO else None
        asset_allocation = self._asset_allocation(assets_view, denominator)
        account_allocation = self._account_allocation(accounts_view, denominator)
        chain_allocation = self._chain_allocation(accounts_view, denominator)
        product_allocation = self._allocations(
            [
                ("spot", "Spot / Cash", spot_value + cash_value),
                ("perp", "Perp", perp_equity),
                ("defi", "DeFi", defi_value),
            ],
            denominator,
        )

        snapshots = list(reversed(list(
            self.session.scalars(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.portfolio_id == portfolio_id, PortfolioSnapshot.as_of <= current_at)
                .order_by(PortfolioSnapshot.as_of.desc())
                .limit(500)
            )
        )))
        periods = self._periods(portfolio_id, current_at, total_nav, all_time_pnl, snapshots)
        pending_review_count = self.session.scalar(
            select(func.count()).select_from(TransferCandidate).where(
                TransferCandidate.portfolio_id == portfolio_id,
                TransferCandidate.status == TransferCandidateStatus.NEEDS_REVIEW,
            )
        ) or 0
        unknown_deposits = self._unknown_deposit_count(portfolio_id, current_at)
        balance_difference_count, max_balance_difference = self._balance_reconciliation(
            cost_positions, accounts, current_at, skip_cost_accounts
        )
        if balance_difference_count:
            valuation_complete = False
            total_nav = None
            warnings.append("Ledger inventory differs from the latest account balance snapshot; NAV is incomplete until reconciled.")
        valued_positions = sum(1 for row in cost_positions if row.effective_cost_usd is not None)
        total_positions = len(cost_positions)
        coverage = None if total_positions == 0 else Decimal(valued_positions) / Decimal(total_positions) * HUNDRED
        warnings = list(dict.fromkeys(warnings))
        health = DashboardHealthRead(
            cost_coverage_percent=money(coverage),
            valued_positions=valued_positions,
            total_positions=total_positions,
            pending_transfer_reviews=pending_review_count,
            unknown_deposits=unknown_deposits,
            incomplete_pnl_records=incomplete_pnl,
            balance_difference_count=balance_difference_count,
            max_balance_difference_percent=money(max_balance_difference),
            valuation_complete=valuation_complete,
            warnings=warnings,
        )

        return DashboardSummaryRead(
            portfolio_id=portfolio.id,
            portfolio_name=portfolio.name,
            base_currency=portfolio.base_currency,
            cost_run_id=run.id,
            cost_method=run.method,
            as_of=current_at,
            total_net_worth_usd=money(total_nav),
            spot_value_usd=money(spot_value) or ZERO,
            perp_equity_usd=money(perp_equity) or ZERO,
            defi_value_usd=money(defi_value) or ZERO,
            cash_usd=money(cash_value) or ZERO,
            debt_usd=money(debt) or ZERO,
            realized_pnl_usd=money(realized),
            unrealized_pnl_usd=money(unrealized),
            fee_expense_usd=money(fee_expense),
            funding_pnl_usd=money(funding_pnl),
            all_time_pnl_usd=money(all_time_pnl),
            adjustment_usd=money(adjustment_total) or ZERO,
            gross_long_usd=money(gross_long) or ZERO,
            gross_short_usd=money(gross_short) or ZERO,
            net_exposure_usd=money(gross_long - gross_short) or ZERO,
            margin_usage_percent=money(margin_usage),
            periods=periods,
            nav_history=snapshots,
            asset_allocation=asset_allocation,
            account_allocation=account_allocation,
            chain_allocation=chain_allocation,
            product_allocation=product_allocation,
            assets=assets_view,
            accounts=accounts_view,
            positions=position_views,
            exposures=exposure_views,
            health=health,
        )

    def capture_snapshot(
        self,
        portfolio_id: UUID,
        as_of: datetime,
        method=None,
        recalculate_cost: bool = True,
    ) -> PortfolioSnapshot:
        target = as_utc(as_of)
        if target > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("snapshot as_of cannot be in the future")
        existing = self.session.scalar(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.portfolio_id == portfolio_id,
                PortfolioSnapshot.as_of == target,
            )
        )
        if existing:
            raise ValueError("portfolio snapshot already exists for this timestamp")
        run_id = None
        if recalculate_cost:
            run = CostBasisService(self.session).calculate(
                portfolio_id,
                CostBasisRunRequest(method=method, as_of=target),
            )
            if run.status == SyncRunStatus.FAILED:
                raise ValueError(run.error_message or "cost basis calculation failed")
            run_id = run.id
        summary = self.summary(portfolio_id, run_id=run_id, as_of=target)
        previous = self.session.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id, PortfolioSnapshot.as_of < target)
            .order_by(PortfolioSnapshot.as_of.desc())
            .limit(1)
        )
        external_flow = None if not previous else self._external_flow(portfolio_id, previous.as_of, target)
        investment_pnl = None
        if previous and previous.total_nav is not None and summary.total_net_worth_usd is not None and external_flow is not None:
            investment_pnl = summary.total_net_worth_usd - previous.total_nav - external_flow
        snapshot = PortfolioSnapshot(
            portfolio_id=portfolio_id,
            source_cost_run_id=summary.cost_run_id,
            total_nav=summary.total_net_worth_usd,
            spot_value=summary.spot_value_usd,
            perp_equity=summary.perp_equity_usd,
            defi_value=summary.defi_value_usd,
            cash=summary.cash_usd,
            debt=summary.debt_usd,
            realized_pnl=summary.realized_pnl_usd,
            unrealized_pnl=summary.unrealized_pnl_usd,
            fee_expense=summary.fee_expense_usd,
            funding_pnl=summary.funding_pnl_usd,
            external_flow=external_flow,
            investment_pnl=investment_pnl,
            valuation_complete=summary.health.valuation_complete,
            data_quality_json={
                "warnings": summary.health.warnings,
                "cost_run_status": self.session.get(CostBasisRun, summary.cost_run_id).status.value,
                "cost_coverage_percent": str(summary.health.cost_coverage_percent) if summary.health.cost_coverage_percent is not None else None,
                "incomplete_pnl_records": summary.health.incomplete_pnl_records,
            },
            as_of=target,
        )
        self.session.add(snapshot)
        self.session.commit()
        self.session.refresh(snapshot)
        return snapshot

    def backfill(self, portfolio_id: UUID, history_start: datetime, history_end: datetime, method=None) -> DashboardBackfillRead:
        start = as_utc(history_start)
        end = as_utc(history_end)
        if start > end:
            raise ValueError("history_start must be before history_end")
        if end - start > timedelta(days=366):
            raise ValueError("dashboard backfill is limited to 366 days")
        now = datetime.now(timezone.utc)
        if start > now + timedelta(minutes=5):
            raise ValueError("history_start cannot be in the future")
        end = min(end, now)
        existing_dates = {
            as_utc(row.as_of).date()
            for row in self.session.scalars(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.portfolio_id == portfolio_id,
                    PortfolioSnapshot.as_of >= start - timedelta(days=1),
                    PortfolioSnapshot.as_of <= end + timedelta(days=1),
                )
            )
        }
        created: list[UUID] = []
        skipped = 0
        partial = 0
        cursor = start.date()
        while cursor <= end.date():
            if cursor in existing_dates:
                skipped += 1
                cursor += timedelta(days=1)
                continue
            target = datetime.combine(cursor, time(23, 59, 59), tzinfo=timezone.utc)
            if target > end:
                target = end
            snapshot = self.capture_snapshot(portfolio_id, target, method=method, recalculate_cost=True)
            created.append(snapshot.id)
            if not snapshot.valuation_complete:
                partial += 1
            cursor += timedelta(days=1)
        return DashboardBackfillRead(
            portfolio_id=portfolio_id,
            created=len(created),
            skipped_existing=skipped,
            partial=partial,
            snapshot_ids=created,
        )

    def _cost_run(self, portfolio_id: UUID, run_id: UUID | None, as_of: datetime | None) -> CostBasisRun:
        if run_id:
            run = self.session.get(CostBasisRun, run_id)
            if not run or run.portfolio_id != portfolio_id:
                raise ValueError("cost basis run not found")
            if run.status not in {SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL}:
                raise ValueError("cost basis run is not complete")
            return run
        statement = select(CostBasisRun).where(
            CostBasisRun.portfolio_id == portfolio_id,
            CostBasisRun.status.in_([SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL]),
        )
        if as_of:
            statement = statement.where(CostBasisRun.as_of <= as_utc(as_of))
        run = self.session.scalar(statement.order_by(CostBasisRun.as_of.desc(), CostBasisRun.started_at.desc()).limit(1))
        if not run:
            raise ValueError("no completed Cost Basis Run; calculate Phase 6 first")
        return run

    def _latest_positions(self, accounts: list[Account], as_of: datetime) -> list[PositionSnapshot]:
        result: list[PositionSnapshot] = []
        for account in accounts:
            marker = self.session.scalar(
                select(RawEvent)
                .where(
                    RawEvent.account_id == account.id,
                    RawEvent.event_kind.in_(["positions", "clearinghouse_state"]),
                    RawEvent.occurred_at <= as_of,
                )
                .order_by(RawEvent.occurred_at.desc(), RawEvent.received_at.desc())
                .limit(1)
            )
            if marker:
                result.extend(
                    self.session.scalars(
                        select(PositionSnapshot).where(PositionSnapshot.source_raw_event_id == marker.id)
                    )
                )
                continue
            latest_at = self.session.scalar(
                select(PositionSnapshot.as_of)
                .where(PositionSnapshot.account_id == account.id, PositionSnapshot.as_of <= as_of)
                .order_by(PositionSnapshot.as_of.desc())
                .limit(1)
            )
            if latest_at:
                result.extend(
                    self.session.scalars(
                        select(PositionSnapshot).where(
                            PositionSnapshot.account_id == account.id,
                            PositionSnapshot.as_of == latest_at,
                        )
                    )
                )
        return result

    def _latest_equities(self, accounts: list[Account], as_of: datetime) -> dict[UUID, AccountEquitySnapshot]:
        result: dict[UUID, AccountEquitySnapshot] = {}
        for account in accounts:
            row = self.session.scalar(
                select(AccountEquitySnapshot)
                .where(AccountEquitySnapshot.account_id == account.id, AccountEquitySnapshot.as_of <= as_of)
                .order_by(AccountEquitySnapshot.as_of.desc())
                .limit(1)
            )
            if row:
                result[account.id] = row
        return result

    def _derivative_account_values(
        self,
        accounts: list[Account],
        positions: list[PositionSnapshot],
        equities: dict[UUID, AccountEquitySnapshot],
        as_of: datetime,
    ) -> tuple[dict[UUID, Decimal], dict[UUID, bool]]:
        by_account: dict[UUID, list[PositionSnapshot]] = defaultdict(list)
        for row in positions:
            by_account[row.account_id].append(row)
        values: dict[UUID, Decimal] = {}
        complete: dict[UUID, bool] = {}
        for account in accounts:
            equity = equities.get(account.id)
            if equity:
                multiplier = self._currency_price(equity.currency, as_of)
                if multiplier is None:
                    values[account.id] = ZERO
                    complete[account.id] = False
                else:
                    values[account.id] = equity.equity * multiplier
                    complete[account.id] = self._is_fresh(equity.as_of, as_of)
                continue
            if not self._is_binance_derivative(account):
                continue
            balance_value, balance_complete = self._latest_balance_value(account.id, as_of)
            upnl_rows = by_account.get(account.id, [])
            upnl = ZERO
            upnl_complete = True
            for row in upnl_rows:
                value = self._position_unrealized_usd(row)
                if value is None:
                    upnl_complete = False
                else:
                    upnl += value
            values[account.id] = balance_value + upnl
            complete[account.id] = balance_complete and upnl_complete
        return values, complete

    def _latest_balance_value(self, account_id: UUID, as_of: datetime) -> tuple[Decimal, bool]:
        latest_at = self.session.scalar(
            select(BalanceSnapshot.as_of)
            .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.as_of <= as_of)
            .order_by(BalanceSnapshot.as_of.desc())
            .limit(1)
        )
        if not latest_at:
            zero_balance_marker = self.session.scalar(
                select(RawEvent.id)
                .where(
                    RawEvent.account_id == account_id,
                    RawEvent.event_kind == "account",
                    RawEvent.occurred_at <= as_of,
                )
                .order_by(RawEvent.occurred_at.desc())
                .limit(1)
            )
            return ZERO, zero_balance_marker is not None
        rows = list(
            self.session.scalars(
                select(BalanceSnapshot).where(
                    BalanceSnapshot.account_id == account_id,
                    BalanceSnapshot.as_of == latest_at,
                )
            )
        )
        total = ZERO
        complete = True
        for row in rows:
            asset = self.session.get(Asset, row.asset_id)
            price = self._asset_price(asset, as_utc(latest_at)) if asset else None
            if price is None:
                complete = False
            else:
                total += row.quantity * price
        return total, complete and self._is_fresh(latest_at, as_of)

    def _perp_unrealized(
        self,
        positions: list[PositionSnapshot],
        equities: dict[UUID, AccountEquitySnapshot],
        as_of: datetime,
    ) -> tuple[Decimal, bool]:
        account_ids = {row.account_id for row in positions} | set(equities)
        total = ZERO
        complete = True
        for account_id in account_ids:
            equity = equities.get(account_id)
            if equity and equity.unrealized_pnl is not None:
                multiplier = self._currency_price(equity.currency, as_utc(equity.as_of))
                if multiplier is None:
                    complete = False
                else:
                    total += equity.unrealized_pnl * multiplier
                    complete = complete and self._is_fresh(equity.as_of, as_of)
                continue
            rows = [row for row in positions if row.account_id == account_id]
            for row in rows:
                value = self._position_unrealized_usd(row)
                if value is None:
                    complete = False
                else:
                    total += value
                    complete = complete and self._is_fresh(row.as_of, as_of)
        return total, complete

    def _position_unrealized_usd(self, row: PositionSnapshot) -> Decimal | None:
        if row.unrealized_pnl is None:
            return None
        margin_asset = row.margin_asset
        if not margin_asset:
            return None
        multiplier = self._currency_price(margin_asset, as_utc(row.as_of))
        return None if multiplier is None else row.unrealized_pnl * multiplier

    def _position_views(self, rows: list[PositionSnapshot], accounts: dict[UUID, Account]) -> list[DashboardPositionRead]:
        result: list[DashboardPositionRead] = []
        for row in rows:
            notional = abs(row.notional) if row.notional is not None else (
                abs(row.quantity * row.mark_price) if row.mark_price is not None else None
            )
            margin = None
            metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            if metadata.get("margin_used") is not None:
                try:
                    margin = Decimal(str(metadata["margin_used"]))
                except Exception:
                    margin = None
            if margin is None and notional is not None and row.leverage is not None and row.leverage > ZERO:
                margin = notional / row.leverage
            account = accounts.get(row.account_id)
            result.append(
                DashboardPositionRead(
                    account_id=row.account_id,
                    account_label=account.label if account else str(row.account_id),
                    product=row.product,
                    symbol=row.symbol,
                    side=self._position_side(row),
                    quantity=money(abs(row.quantity)) or ZERO,
                    entry_price=money(row.entry_price),
                    mark_price=money(row.mark_price),
                    notional_usd=money(notional),
                    unrealized_pnl_usd=money(self._position_unrealized_usd(row)),
                    leverage=money(row.leverage),
                    margin_usd=money(margin),
                    liquidation_price=money(row.liquidation_price),
                    as_of=as_utc(row.as_of),
                )
            )
        return sorted(result, key=lambda item: (item.account_label, item.symbol, item.side))

    def _exposures(
        self,
        spot_rows: list[PositionCostSnapshot],
        position_rows: list[PositionSnapshot],
        assets: dict[UUID, Asset],
        skip_cost_accounts: set[UUID],
    ) -> list[DashboardExposureRead]:
        data: dict[str, dict] = {}
        for row in spot_rows:
            if row.account_id in skip_cost_accounts:
                continue
            asset = assets.get(row.asset_id) or self.session.get(Asset, row.asset_id)
            if not asset or self._is_cash(asset):
                continue
            key = asset.canonical_symbol.upper()
            item = data.setdefault(
                key,
                {"asset_id": asset.id, "spot": ZERO, "long": ZERO, "short": ZERO, "gross_long": ZERO, "gross_short": ZERO},
            )
            item["spot"] += row.quantity
            item["gross_long"] += row.market_value_usd or ZERO
        for row in position_rows:
            symbol = self._base_symbol(row)
            asset = self.session.scalar(select(Asset).where(Asset.canonical_symbol == symbol, Asset.chain_id.is_(None)).limit(1))
            item = data.setdefault(
                symbol,
                {"asset_id": asset.id if asset else None, "spot": ZERO, "long": ZERO, "short": ZERO, "gross_long": ZERO, "gross_short": ZERO},
            )
            notional = abs(row.notional) if row.notional is not None else (
                abs(row.quantity * row.mark_price) if row.mark_price is not None else ZERO
            )
            base_quantity = abs(row.quantity)
            metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            if metadata.get("source_quantity_unit") == "contracts" and row.mark_price and row.mark_price > ZERO and notional > ZERO:
                base_quantity = notional / row.mark_price
            if self._position_side(row) == "SHORT":
                item["short"] += base_quantity
                item["gross_short"] += notional
            else:
                item["long"] += base_quantity
                item["gross_long"] += notional
        result = []
        for symbol, item in data.items():
            result.append(
                DashboardExposureRead(
                    asset_id=item["asset_id"],
                    symbol=symbol,
                    spot_quantity=money(item["spot"]) or ZERO,
                    perp_long_quantity=money(item["long"]) or ZERO,
                    perp_short_quantity=money(item["short"]) or ZERO,
                    net_quantity=money(item["spot"] + item["long"] - item["short"]) or ZERO,
                    gross_long_usd=money(item["gross_long"]) or ZERO,
                    gross_short_usd=money(item["gross_short"]) or ZERO,
                    net_exposure_usd=money(item["gross_long"] - item["gross_short"]) or ZERO,
                )
            )
        return sorted(result, key=lambda item: abs(item.net_exposure_usd), reverse=True)

    def _asset_views(
        self,
        run: CostBasisRun,
        grouped: dict[UUID, list[PositionCostSnapshot]],
        assets: dict[UUID, Asset],
        records: list[RealizedPnlRecord],
    ) -> list[DashboardAssetRead]:
        pnl_by_asset: dict[UUID, list[RealizedPnlRecord]] = defaultdict(list)
        for record in records:
            pnl_by_asset[record.asset_id].append(record)
        result: list[DashboardAssetRead] = []
        for asset_id, rows in grouped.items():
            asset = assets.get(asset_id) or self.session.get(Asset, asset_id)
            quantity = sum((row.quantity for row in rows), ZERO)
            calculated = None if any(row.calculated_cost_usd is None for row in rows) else sum((row.calculated_cost_usd or ZERO for row in rows), ZERO)
            effective = None if any(row.effective_cost_usd is None for row in rows) else sum((row.effective_cost_usd or ZERO for row in rows), ZERO)
            account_manual = any(row.manual_cost_usd is not None for row in rows)
            manual = effective if account_manual else None
            portfolio_override = self.session.scalar(
                select(CostBasisOverride.total_cost_usd)
                .where(
                    CostBasisOverride.portfolio_id == run.portfolio_id,
                    CostBasisOverride.asset_id == asset_id,
                    CostBasisOverride.account_id.is_(None),
                    CostBasisOverride.ledger_event_id.is_(None),
                    CostBasisOverride.override_type == CostOverrideType.POSITION_TOTAL,
                    CostBasisOverride.created_at <= run.as_of,
                )
                .order_by(CostBasisOverride.created_at.desc(), CostBasisOverride.id.desc())
                .limit(1)
            )
            if portfolio_override is not None:
                manual = portfolio_override
                effective = portfolio_override
            # Do not expose the price persisted with a historical cost-basis
            # run as if it were live. Asset rows follow the same fresh-price
            # rule as the total NAV.
            price = self._asset_price(asset, as_utc(run.as_of)) if asset else None
            market = None if price is None else quantity * price
            unrealized = None if market is None or effective is None else market - effective
            unrealized_pct = None if unrealized is None or effective in {None, ZERO} else unrealized / effective * HUNDRED
            asset_records = pnl_by_asset[asset_id]
            realized = None if any(row.realized_pnl_usd is None for row in asset_records) else sum((row.realized_pnl_usd or ZERO for row in asset_records), ZERO)
            lot_count = len(
                list(
                    self.session.scalars(
                        select(CostLot.id).where(
                            CostLot.run_id == run.id,
                            CostLot.asset_id == asset_id,
                            CostLot.remaining_quantity > ZERO,
                        )
                    )
                )
            )
            result.append(
                DashboardAssetRead(
                    asset_id=asset_id,
                    symbol=asset.canonical_symbol if asset else str(asset_id),
                    name=asset.name if asset else str(asset_id),
                    quantity=money(quantity) or ZERO,
                    calculated_cost_usd=money(calculated),
                    manual_cost_usd=money(manual),
                    effective_cost_usd=money(effective),
                    average_cost_usd=money(None if effective is None or quantity == ZERO else effective / quantity),
                    current_price_usd=money(price),
                    market_value_usd=money(market),
                    unrealized_pnl_usd=money(unrealized),
                    unrealized_pnl_percent=money(unrealized_pct),
                    realized_pnl_usd=money(realized),
                    account_count=len({row.account_id for row in rows}),
                    open_lot_count=lot_count,
                    valuation_complete=market is not None and effective is not None,
                )
            )
        return sorted(result, key=lambda row: row.market_value_usd or ZERO, reverse=True)

    def _account_views(
        self,
        accounts: list[Account],
        account_spot: dict[UUID, Decimal],
        account_cash: dict[UUID, Decimal],
        derivative_values: dict[UUID, Decimal],
        derivative_complete: dict[UUID, bool],
        account_complete: dict[UUID, bool],
        equities: dict[UUID, AccountEquitySnapshot],
        records: list[RealizedPnlRecord],
        events: dict[UUID, LedgerEvent],
        adjustments: list[PnlAdjustment],
        position_rows: list[PositionSnapshot],
        spot_unrealized: dict[UUID, Decimal | None],
        as_of: datetime,
    ) -> list[DashboardAccountRead]:
        records_by_account: dict[UUID, list[RealizedPnlRecord]] = defaultdict(list)
        for record in records:
            records_by_account[record.account_id].append(record)
        adjustments_by_account: dict[UUID, Decimal] = defaultdict(lambda: ZERO)
        for adjustment in adjustments:
            if adjustment.account_id:
                adjustments_by_account[adjustment.account_id] += adjustment.amount_usd
        last_sync = self._last_sync_times(accounts, as_of)
        result = []
        for account in accounts:
            rows = records_by_account[account.id]
            realized = None if any(row.realized_pnl_usd is None for row in rows) else sum((row.realized_pnl_usd or ZERO for row in rows), ZERO) + adjustments_by_account[account.id]
            fees = sum((row.fee_usd for row in rows), ZERO)
            funding_rows = [row for row in rows if events.get(row.ledger_event_id) and events[row.ledger_event_id].event_type == LedgerEventType.FUNDING]
            funding = None if any(row.realized_pnl_usd is None for row in funding_rows) else sum((row.realized_pnl_usd or ZERO for row in funding_rows), ZERO)
            equity = equities.get(account.id)
            account_positions = [row for row in position_rows if row.account_id == account.id]
            perp_unrealized = (
                equity.unrealized_pnl * self._currency_price(equity.currency, as_of)
                if equity and equity.unrealized_pnl is not None and self._currency_price(equity.currency, as_of) is not None
                else None
            ) if equity else (
                None if any(row.unrealized_pnl is None for row in account_positions) else sum((row.unrealized_pnl or ZERO for row in account_positions), ZERO)
            )
            spot_component = spot_unrealized.get(account.id, ZERO)
            account_unrealized = None if perp_unrealized is None or spot_component is None else perp_unrealized + spot_component
            total = account_spot[account.id] + account_cash[account.id] + derivative_values.get(account.id, ZERO)
            complete = account_complete.get(account.id, True) and derivative_complete.get(account.id, True)
            result.append(
                DashboardAccountRead(
                    account_id=account.id,
                    label=account.label,
                    provider=account.provider,
                    kind=account.kind.value,
                    chain_id=account.chain_id,
                    spot_value_usd=money(account_spot[account.id]) or ZERO,
                    cash_usd=money(account_cash[account.id]) or ZERO,
                    perp_equity_usd=money(derivative_values.get(account.id, ZERO)) or ZERO,
                    total_equity_usd=money(total) if complete else None,
                    realized_pnl_usd=money(realized),
                    unrealized_pnl_usd=money(account_unrealized),
                    funding_pnl_usd=money(funding),
                    fee_expense_usd=money(fees),
                    margin_used_usd=money(equity.margin_used * self._currency_price(equity.currency, as_of)) if equity and equity.margin_used is not None and self._currency_price(equity.currency, as_of) is not None else None,
                    last_synced_at=last_sync.get(account.id),
                    valuation_complete=complete,
                )
            )
        return sorted(result, key=lambda row: row.total_equity_usd or ZERO, reverse=True)

    @staticmethod
    def _spot_unrealized_by_account(
        positions: list[PositionCostSnapshot],
        skip_accounts: set[UUID],
    ) -> dict[UUID, Decimal | None]:
        grouped: dict[UUID, list[PositionCostSnapshot]] = defaultdict(list)
        for row in positions:
            if row.account_id not in skip_accounts:
                grouped[row.account_id].append(row)
        result: dict[UUID, Decimal | None] = {}
        for account_id, rows in grouped.items():
            result[account_id] = None if any(row.unrealized_pnl_usd is None for row in rows) else sum((row.unrealized_pnl_usd or ZERO for row in rows), ZERO)
        return result

    def _last_sync_times(self, accounts: list[Account], as_of: datetime) -> dict[UUID, datetime]:
        result: dict[UUID, datetime] = {}
        for account in accounts:
            values: list[datetime] = []
            cursor = self.session.scalar(
                select(SyncCursor.last_synced_at)
                .where(SyncCursor.account_id == account.id, SyncCursor.last_synced_at <= as_of)
                .order_by(SyncCursor.last_synced_at.desc())
                .limit(1)
            )
            if cursor:
                values.append(as_utc(cursor))
            for model in (BalanceSnapshot, AccountEquitySnapshot, PositionSnapshot):
                latest = self.session.scalar(
                    select(model.as_of)
                    .where(model.account_id == account.id, model.as_of <= as_of)
                    .order_by(model.as_of.desc())
                    .limit(1)
                )
                if latest:
                    values.append(as_utc(latest))
            if values:
                result[account.id] = max(values)
        return result

    def _periods(
        self,
        portfolio_id: UUID,
        as_of: datetime,
        current_nav: Decimal | None,
        all_time_pnl: Decimal | None,
        snapshots: list[PortfolioSnapshot],
    ) -> list[DashboardPeriodRead]:
        day_start = datetime.combine(as_of.date(), time.min, tzinfo=timezone.utc)
        windows = [
            ("1D", "Today", day_start),
            ("7D", "7D", as_of - timedelta(days=7)),
            ("30D", "30D", as_of - timedelta(days=30)),
        ]
        result: list[DashboardPeriodRead] = []
        for key, label, start in windows:
            baseline = next((row for row in reversed(snapshots) if as_utc(row.as_of) <= start), None)
            flow = None if not baseline else self._external_flow(portfolio_id, baseline.as_of, as_of)
            complete = bool(
                baseline
                and baseline.total_nav is not None
                and current_nav is not None
                and flow is not None
                and baseline.valuation_complete
            )
            pnl = current_nav - baseline.total_nav - flow if complete else None
            pct = None if pnl is None or not baseline or baseline.total_nav in {None, ZERO} else pnl / baseline.total_nav * HUNDRED
            result.append(
                DashboardPeriodRead(
                    key=key,
                    label=label,
                    pnl_usd=money(pnl),
                    return_percent=money(pct),
                    external_flow_usd=money(flow),
                    baseline_nav_usd=money(baseline.total_nav) if baseline else None,
                    baseline_as_of=as_utc(baseline.as_of) if baseline else None,
                    complete=complete,
                )
            )
        first = snapshots[0] if snapshots else None
        all_flow = None if not first else self._external_flow(portfolio_id, first.as_of, as_of)
        all_value = all_time_pnl
        capital = None if current_nav is None or all_value is None else current_nav - all_value
        all_pct = None if capital is None or capital <= ZERO else all_value / capital * HUNDRED
        all_complete = all_value is not None
        result.append(
            DashboardPeriodRead(
                key="ALL",
                label="All time",
                pnl_usd=money(all_value),
                return_percent=money(all_pct),
                external_flow_usd=money(all_flow),
                baseline_nav_usd=money(first.total_nav) if first else None,
                baseline_as_of=as_utc(first.as_of) if first else None,
                complete=all_complete,
            )
        )
        return result

    def _external_flow(self, portfolio_id: UUID, start: datetime, end: datetime) -> Decimal | None:
        groups = list(
            self.session.scalars(
                select(TransferGroup).where(
                    TransferGroup.portfolio_id == portfolio_id,
                    TransferGroup.status.in_(list(ACTIVE_TRANSFER_STATUSES)),
                )
            )
        )
        internal_events = {group.source_event_id for group in groups} | {group.destination_event_id for group in groups}
        events = list(
            self.session.scalars(
                select(LedgerEvent).where(
                    LedgerEvent.portfolio_id == portfolio_id,
                    LedgerEvent.status == EventStatus.POSTED,
                    LedgerEvent.event_type.in_(list(EXTERNAL_IN_TYPES | EXTERNAL_OUT_TYPES)),
                    LedgerEvent.occurred_at > start,
                    LedgerEvent.occurred_at <= end,
                )
            )
        )
        total = ZERO
        complete = True
        for event in events:
            if event.id in internal_events:
                continue
            entries = list(
                self.session.scalars(
                    select(LedgerEntry).where(
                        LedgerEntry.ledger_event_id == event.id,
                        LedgerEntry.fee_flag.is_(False),
                    )
                )
            )
            expected_direction = EntryDirection.CREDIT if event.event_type in EXTERNAL_IN_TYPES else EntryDirection.DEBIT
            sign = Decimal("1") if expected_direction == EntryDirection.CREDIT else Decimal("-1")
            for entry in entries:
                if entry.direction != expected_direction:
                    continue
                asset = self.session.get(Asset, entry.asset_id)
                price = entry.unit_price_usd if entry.unit_price_usd is not None else self._asset_price(asset, as_utc(event.occurred_at)) if asset else None
                if price is None:
                    complete = False
                else:
                    total += sign * entry.quantity * price
        return total if complete else None

    def _unknown_deposit_count(self, portfolio_id: UUID, as_of: datetime) -> int:
        group_destinations = set(
            self.session.scalars(
                select(TransferGroup.destination_event_id).where(
                    TransferGroup.portfolio_id == portfolio_id,
                    TransferGroup.status.in_(list(ACTIVE_TRANSFER_STATUSES)),
                )
            )
        )
        events = list(
            self.session.scalars(
                select(LedgerEvent.id).where(
                    LedgerEvent.portfolio_id == portfolio_id,
                    LedgerEvent.status == EventStatus.POSTED,
                    LedgerEvent.event_type.in_(list(EXTERNAL_IN_TYPES)),
                    LedgerEvent.occurred_at <= as_of,
                )
            )
        )
        count = 0
        for event_id in events:
            if event_id in group_destinations:
                continue
            override = self.session.scalar(
                select(CostBasisOverride.id).where(
                    CostBasisOverride.portfolio_id == portfolio_id,
                    CostBasisOverride.ledger_event_id == event_id,
                    CostBasisOverride.override_type == CostOverrideType.EVENT_TOTAL,
                ).limit(1)
            )
            if not override:
                count += 1
        return count

    def _balance_reconciliation(
        self,
        positions: list[PositionCostSnapshot],
        accounts: list[Account],
        as_of: datetime,
        skip_accounts: set[UUID],
    ) -> tuple[int, Decimal | None]:
        ledger = {(row.account_id, row.asset_id): row.quantity for row in positions if row.account_id not in skip_accounts}
        differences: list[Decimal] = []
        for account in accounts:
            if account.id in skip_accounts:
                continue
            latest_at = self.session.scalar(
                select(BalanceSnapshot.as_of)
                .where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.as_of <= as_of)
                .order_by(BalanceSnapshot.as_of.desc())
                .limit(1)
            )
            if not latest_at:
                continue
            balances = list(
                self.session.scalars(
                    select(BalanceSnapshot).where(
                        BalanceSnapshot.account_id == account.id,
                        BalanceSnapshot.as_of == latest_at,
                    )
                )
            )
            api = {row.asset_id: row.quantity for row in balances}
            asset_ids = set(api) | {asset_id for account_id, asset_id in ledger if account_id == account.id}
            for asset_id in asset_ids:
                api_quantity = api.get(asset_id, ZERO)
                ledger_quantity = ledger.get((account.id, asset_id), ZERO)
                denominator = max(abs(api_quantity), Decimal("0.000000000000000001"))
                difference = abs(ledger_quantity - api_quantity) / denominator * HUNDRED
                if difference > Decimal("0.1"):
                    differences.append(difference)
        return len(differences), max(differences) if differences else ZERO

    def _stale_balance_accounts(
        self,
        accounts: list[Account],
        as_of: datetime,
        skip_accounts: set[UUID],
    ) -> list[Account]:
        stale: list[Account] = []
        for account in accounts:
            if account.id in skip_accounts:
                continue
            latest_at = self.session.scalar(
                select(BalanceSnapshot.as_of)
                .where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.as_of <= as_of)
                .order_by(BalanceSnapshot.as_of.desc())
                .limit(1)
            )
            if latest_at and not self._is_fresh(latest_at, as_of):
                stale.append(account)
        return stale

    def _asset_allocation(self, assets: list[DashboardAssetRead], denominator: Decimal | None) -> list[DashboardAllocationRead]:
        return self._allocations(
            [(str(asset.asset_id), asset.symbol, asset.market_value_usd or ZERO) for asset in assets if asset.market_value_usd is not None],
            denominator,
        )

    def _account_allocation(self, accounts: list[DashboardAccountRead], denominator: Decimal | None) -> list[DashboardAllocationRead]:
        return self._allocations(
            [(str(account.account_id), account.label, account.total_equity_usd or ZERO) for account in accounts if account.total_equity_usd is not None],
            denominator,
        )

    def _chain_allocation(self, accounts: list[DashboardAccountRead], denominator: Decimal | None) -> list[DashboardAllocationRead]:
        grouped: dict[str, Decimal] = defaultdict(lambda: ZERO)
        labels: dict[str, str] = {}
        for account in accounts:
            if account.total_equity_usd is None:
                continue
            key = account.chain_id or f"offchain:{account.provider}"
            label = f"Chain {account.chain_id}" if account.chain_id else account.provider.title()
            grouped[key] += account.total_equity_usd
            labels[key] = label
        return self._allocations([(key, labels[key], value) for key, value in grouped.items()], denominator)

    def _allocations(
        self,
        rows: Iterable[tuple[str, str, Decimal]],
        denominator: Decimal | None,
    ) -> list[DashboardAllocationRead]:
        result = [
            DashboardAllocationRead(
                key=key,
                label=label,
                value_usd=money(value) or ZERO,
                percentage=money(None if denominator is None or denominator == ZERO else value / denominator * HUNDRED),
            )
            for key, label, value in rows
            if value != ZERO
        ]
        return sorted(result, key=lambda item: item.value_usd, reverse=True)

    def _margin_used(self, equities: dict[UUID, AccountEquitySnapshot], positions: list[DashboardPositionRead]) -> Decimal:
        equity_accounts = {account_id for account_id, row in equities.items() if row.margin_used is not None}
        total = sum((row.margin_used or ZERO for row in equities.values()), ZERO)
        total += sum((row.margin_usd or ZERO for row in positions if row.account_id not in equity_accounts), ZERO)
        return total

    def _asset_price(self, asset: Asset, at: datetime) -> Decimal | None:
        if asset.asset_type == AssetType.FIAT and asset.canonical_symbol == "USD":
            return Decimal("1")
        key = (asset.id, at)
        if key not in self._price_cache:
            row = self.session.scalar(
                select(AssetPrice)
                .where(AssetPrice.asset_id == asset.id, AssetPrice.as_of <= at)
                .order_by(AssetPrice.as_of.desc(), AssetPrice.created_at.desc())
                .limit(1)
            )
            self._price_cache[key] = row.price_usd if row and self._is_fresh(row.as_of, at) else None
        return self._price_cache[key]

    def _currency_price(self, symbol: str, at: datetime) -> Decimal | None:
        normalized = symbol.upper()
        asset = self.session.scalar(select(Asset).where(Asset.canonical_symbol == normalized, Asset.chain_id.is_(None)).limit(1))
        return self._asset_price(asset, at) if asset else None

    @staticmethod
    def _is_cash(asset: Asset) -> bool:
        # Stablecoins are presented as cash, but their actual USD price still
        # comes from AssetPrice (no hard-coded one-dollar valuation).
        return asset.asset_type == AssetType.STABLECOIN or (
            asset.asset_type == AssetType.FIAT and asset.canonical_symbol == "USD"
        )

    def _is_fresh(self, observed_at: datetime, as_of: datetime) -> bool:
        return as_utc(as_of) - as_utc(observed_at) <= self._max_valuation_age

    @staticmethod
    def _is_binance_derivative(account: Account) -> bool:
        identity = (account.external_account_id or "").lower()
        return account.provider.lower() == "binance" and (identity.endswith(":usdm") or identity.endswith(":coinm"))

    @staticmethod
    def _position_side(row: PositionSnapshot) -> str:
        side = (row.position_side or "").upper()
        if side in {"SHORT", "LONG"}:
            return side
        return "SHORT" if row.quantity < ZERO else "LONG"

    @staticmethod
    def _base_symbol(row: PositionSnapshot) -> str:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        base = str(metadata.get("base_asset") or "").upper()
        if base:
            return base
        symbol = row.symbol.upper()
        for suffix in ("USDT", "USDC", "USD", "PERP"):
            if symbol.endswith(suffix) and len(symbol) > len(suffix):
                return symbol[: -len(suffix)].rstrip("-_/:")
        return symbol.split("-")[0]
