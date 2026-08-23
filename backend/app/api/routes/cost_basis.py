from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.db import get_session
from app.models import (
    Account,
    Asset,
    AssetPrice,
    CostBasisOverride,
    CostBasisRun,
    CostLot,
    CostLotConsumption,
    CostOverrideType,
    LedgerEvent,
    LedgerEntry,
    PnlAdjustment,
    Portfolio,
    PositionCostSnapshot,
    RealizedPnlRecord,
    SyncRunStatus,
)
from app.schemas import (
    AssetCostSummaryRead,
    AssetPriceCreate,
    AssetPriceRead,
    CostBasisOverrideCreate,
    CostBasisOverrideRead,
    CostBasisRunRead,
    CostBasisRunRequest,
    CostLotConsumptionRead,
    CostLotRead,
    PnlAdjustmentCreate,
    PnlAdjustmentRead,
    PnlSummaryRead,
    PositionCostRead,
    RealizedPnlRead,
)
from app.services.cost_basis import CostBasisService
from app.services.security import add_security_event

router = APIRouter()
ZERO = Decimal("0")
MONEY_QUANTUM = Decimal("0.000000000000000001")


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(MONEY_QUANTUM)


def _run(session: Session, portfolio_id: UUID, run_id: UUID | None) -> CostBasisRun:
    if run_id:
        run = session.get(CostBasisRun, run_id)
        if not run or run.portfolio_id != portfolio_id:
            raise HTTPException(status_code=404, detail="cost basis run not found")
        return run
    run = session.scalar(
        select(CostBasisRun)
        .where(
            CostBasisRun.portfolio_id == portfolio_id,
            CostBasisRun.status.in_([SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL]),
        )
        .order_by(CostBasisRun.started_at.desc(), CostBasisRun.id.desc())
        .limit(1)
    )
    if not run:
        raise HTTPException(status_code=404, detail="no completed cost basis run found")
    return run


@router.post("/portfolios/{portfolio_id}/calculate", response_model=CostBasisRunRead)
def calculate(portfolio_id: UUID, payload: CostBasisRunRequest, session: Session = Depends(get_session)) -> CostBasisRun:
    try:
        return CostBasisService(session).calculate(portfolio_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/portfolios/{portfolio_id}/runs", response_model=list[CostBasisRunRead])
def list_runs(portfolio_id: UUID, limit: int = Query(default=20, ge=1, le=100), session: Session = Depends(get_session)) -> list[CostBasisRun]:
    return list(
        session.scalars(
            select(CostBasisRun)
            .where(CostBasisRun.portfolio_id == portfolio_id)
            .order_by(CostBasisRun.started_at.desc())
            .limit(limit)
        )
    )


@router.get("/portfolios/{portfolio_id}/positions", response_model=list[PositionCostRead])
def list_positions(
    portfolio_id: UUID,
    run_id: UUID | None = None,
    account_id: UUID | None = None,
    asset_id: UUID | None = None,
    session: Session = Depends(get_session),
) -> list[PositionCostSnapshot]:
    run = _run(session, portfolio_id, run_id)
    statement = select(PositionCostSnapshot).where(PositionCostSnapshot.run_id == run.id)
    if account_id:
        statement = statement.where(PositionCostSnapshot.account_id == account_id)
    if asset_id:
        statement = statement.where(PositionCostSnapshot.asset_id == asset_id)
    return list(session.scalars(statement.order_by(PositionCostSnapshot.asset_id, PositionCostSnapshot.account_id)))


@router.get("/portfolios/{portfolio_id}/assets", response_model=list[AssetCostSummaryRead])
def asset_summaries(portfolio_id: UUID, run_id: UUID | None = None, session: Session = Depends(get_session)) -> list[AssetCostSummaryRead]:
    run = _run(session, portfolio_id, run_id)
    snapshots = list(session.scalars(select(PositionCostSnapshot).where(PositionCostSnapshot.run_id == run.id)))
    grouped: dict[UUID, list[PositionCostSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[snapshot.asset_id].append(snapshot)
    pnl_by_asset: dict[UUID, list[RealizedPnlRecord]] = defaultdict(list)
    for record in session.scalars(select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id)):
        pnl_by_asset[record.asset_id].append(record)

    result: list[AssetCostSummaryRead] = []
    for asset_id, rows in grouped.items():
        asset = session.get(Asset, asset_id)
        quantity = sum((row.quantity for row in rows), ZERO)
        calculated = None if any(row.calculated_cost_usd is None for row in rows) else sum((row.calculated_cost_usd or ZERO for row in rows), ZERO)
        account_manual = any(row.manual_cost_usd is not None for row in rows)
        account_effective = None if any(row.effective_cost_usd is None for row in rows) else sum((row.effective_cost_usd or ZERO for row in rows), ZERO)
        portfolio_manual = session.scalar(
            select(CostBasisOverride.total_cost_usd)
            .where(
                CostBasisOverride.portfolio_id == portfolio_id,
                CostBasisOverride.asset_id == asset_id,
                CostBasisOverride.account_id.is_(None),
                CostBasisOverride.ledger_event_id.is_(None),
                CostBasisOverride.override_type == CostOverrideType.POSITION_TOTAL,
                CostBasisOverride.created_at <= run.as_of,
            )
            .order_by(CostBasisOverride.created_at.desc(), CostBasisOverride.id.desc())
            .limit(1)
        )
        manual = portfolio_manual if portfolio_manual is not None else (account_effective if account_manual else None)
        effective = portfolio_manual if portfolio_manual is not None else account_effective
        price = next((row.market_price_usd for row in rows if row.market_price_usd is not None), None)
        market_value = None if price is None else quantity * price
        unrealized = None if market_value is None or effective is None else market_value - effective
        percent = None if unrealized is None or not effective else unrealized / effective * Decimal("100")
        pnl_values = [record.realized_pnl_usd for record in pnl_by_asset[asset_id]]
        realized = None if any(value is None for value in pnl_values) else sum((value or ZERO for value in pnl_values), ZERO)
        result.append(
            AssetCostSummaryRead(
                run_id=run.id,
                portfolio_id=portfolio_id,
                asset_id=asset_id,
                symbol=asset.canonical_symbol if asset else str(asset_id),
                quantity=_money(quantity),
                calculated_cost_usd=_money(calculated),
                manual_cost_usd=_money(manual),
                effective_cost_usd=_money(effective),
                average_unit_cost_usd=_money(None if effective is None or quantity == ZERO else effective / quantity),
                market_price_usd=_money(price),
                market_value_usd=_money(market_value),
                unrealized_pnl_usd=_money(unrealized),
                unrealized_pnl_percent=_money(percent),
                realized_pnl_usd=_money(realized),
            )
        )
    return sorted(result, key=lambda row: row.symbol)


@router.get("/portfolios/{portfolio_id}/lots", response_model=list[CostLotRead])
def list_lots(
    portfolio_id: UUID,
    run_id: UUID | None = None,
    asset_id: UUID | None = None,
    open_only: bool = True,
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_session),
) -> list[CostLot]:
    run = _run(session, portfolio_id, run_id)
    statement = select(CostLot).where(CostLot.run_id == run.id)
    if asset_id:
        statement = statement.where(CostLot.asset_id == asset_id)
    if open_only:
        statement = statement.where(CostLot.remaining_quantity > ZERO)
    return list(session.scalars(statement.order_by(CostLot.acquired_at.asc()).limit(limit)))


@router.get("/portfolios/{portfolio_id}/consumptions", response_model=list[CostLotConsumptionRead])
def list_consumptions(
    portfolio_id: UUID,
    run_id: UUID | None = None,
    event_id: UUID | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_session),
) -> list[CostLotConsumption]:
    run = _run(session, portfolio_id, run_id)
    statement = select(CostLotConsumption).where(CostLotConsumption.run_id == run.id)
    if event_id:
        statement = statement.where(CostLotConsumption.ledger_event_id == event_id)
    return list(session.scalars(statement.order_by(CostLotConsumption.occurred_at.desc()).limit(limit)))


@router.get("/portfolios/{portfolio_id}/realized-pnl", response_model=list[RealizedPnlRead])
def list_realized_pnl(
    portfolio_id: UUID,
    run_id: UUID | None = None,
    category: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_session),
) -> list[RealizedPnlRecord]:
    run = _run(session, portfolio_id, run_id)
    statement = select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id)
    if category:
        statement = statement.where(RealizedPnlRecord.category == category)
    return list(session.scalars(statement.order_by(RealizedPnlRecord.occurred_at.desc()).limit(limit)))


@router.post(
    "/overrides",
    response_model=CostBasisOverrideRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def create_override(payload: CostBasisOverrideCreate, request: Request, session: Session = Depends(get_session)) -> CostBasisOverride:
    _validate_scope(session, payload.portfolio_id, payload.asset_id, payload.account_id, payload.ledger_event_id)
    override = CostBasisOverride(**payload.model_dump(), created_by_user_id=request.state.user.id)
    session.add(override)
    session.flush()
    add_security_event(session, request, "cost_basis_override_created", request.state.user.id, {"override_id": str(override.id)})
    session.commit()
    session.refresh(override)
    return override


@router.get("/portfolios/{portfolio_id}/overrides", response_model=list[CostBasisOverrideRead])
def list_overrides(portfolio_id: UUID, asset_id: UUID | None = None, session: Session = Depends(get_session)) -> list[CostBasisOverride]:
    statement = select(CostBasisOverride).where(CostBasisOverride.portfolio_id == portfolio_id)
    if asset_id:
        statement = statement.where(CostBasisOverride.asset_id == asset_id)
    return list(session.scalars(statement.order_by(CostBasisOverride.created_at.desc())))


@router.post(
    "/prices",
    response_model=AssetPriceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def create_price(payload: AssetPriceCreate, request: Request, session: Session = Depends(get_session)) -> AssetPrice:
    if not session.get(Asset, payload.asset_id):
        raise HTTPException(status_code=404, detail="asset not found")
    price = AssetPrice(**payload.model_dump())
    session.add(price)
    session.flush()
    add_security_event(session, request, "asset_price_created", request.state.user.id, {"price_id": str(price.id)})
    session.commit()
    session.refresh(price)
    return price


@router.get("/prices", response_model=list[AssetPriceRead])
def list_prices(asset_id: UUID | None = None, limit: int = Query(default=200, ge=1, le=2000), session: Session = Depends(get_session)) -> list[AssetPrice]:
    statement = select(AssetPrice)
    if asset_id:
        statement = statement.where(AssetPrice.asset_id == asset_id)
    return list(session.scalars(statement.order_by(AssetPrice.as_of.desc()).limit(limit)))


@router.post(
    "/pnl-adjustments",
    response_model=PnlAdjustmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def create_pnl_adjustment(payload: PnlAdjustmentCreate, request: Request, session: Session = Depends(get_session)) -> PnlAdjustment:
    _validate_scope(session, payload.portfolio_id, payload.asset_id, payload.account_id, None, allow_empty_asset=True)
    adjustment = PnlAdjustment(**payload.model_dump(), created_by_user_id=request.state.user.id)
    session.add(adjustment)
    session.flush()
    add_security_event(session, request, "pnl_adjustment_created", request.state.user.id, {"adjustment_id": str(adjustment.id)})
    session.commit()
    session.refresh(adjustment)
    return adjustment


@router.get("/portfolios/{portfolio_id}/pnl-adjustments", response_model=list[PnlAdjustmentRead])
def list_pnl_adjustments(portfolio_id: UUID, session: Session = Depends(get_session)) -> list[PnlAdjustment]:
    return list(session.scalars(select(PnlAdjustment).where(PnlAdjustment.portfolio_id == portfolio_id).order_by(PnlAdjustment.occurred_at.desc())))


@router.get("/portfolios/{portfolio_id}/pnl-summary", response_model=PnlSummaryRead)
def pnl_summary(portfolio_id: UUID, run_id: UUID | None = None, session: Session = Depends(get_session)) -> PnlSummaryRead:
    run = _run(session, portfolio_id, run_id)
    records = list(session.scalars(select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id)))
    incomplete = sum(1 for record in records if record.realized_pnl_usd is None)
    system = None if incomplete else sum((record.realized_pnl_usd or ZERO for record in records), ZERO)
    adjustments = list(session.scalars(select(PnlAdjustment).where(PnlAdjustment.portfolio_id == portfolio_id, PnlAdjustment.occurred_at <= run.as_of)))
    adjustment = sum((row.amount_usd for row in adjustments), ZERO)
    return PnlSummaryRead(
        run_id=run.id,
        portfolio_id=portfolio_id,
        system_realized_pnl_usd=system,
        adjustment_usd=adjustment,
        final_realized_pnl_usd=None if system is None else system + adjustment,
        incomplete_records=incomplete,
    )


def _validate_scope(
    session: Session,
    portfolio_id: UUID,
    asset_id: UUID | None,
    account_id: UUID | None,
    event_id: UUID | None,
    *,
    allow_empty_asset: bool = False,
) -> None:
    if not session.get(Portfolio, portfolio_id):
        raise HTTPException(status_code=404, detail="portfolio not found")
    if asset_id and not session.get(Asset, asset_id):
        raise HTTPException(status_code=404, detail="asset not found")
    if not asset_id and not allow_empty_asset:
        raise HTTPException(status_code=422, detail="asset is required")
    if account_id:
        account = session.get(Account, account_id)
        if not account or account.portfolio_id != portfolio_id:
            raise HTTPException(status_code=422, detail="account does not belong to portfolio")
    if event_id:
        event = session.get(LedgerEvent, event_id)
        if not event or event.portfolio_id != portfolio_id:
            raise HTTPException(status_code=422, detail="ledger event does not belong to portfolio")
        entry_statement = select(LedgerEntry.id).where(
            LedgerEntry.ledger_event_id == event_id,
            LedgerEntry.asset_id == asset_id,
        )
        if account_id:
            entry_statement = entry_statement.where(LedgerEntry.account_id == account_id)
        if session.scalar(entry_statement.limit(1)) is None:
            raise HTTPException(status_code=422, detail="override target does not match a ledger entry")
