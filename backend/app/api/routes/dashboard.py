from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import PortfolioSnapshot
from app.schemas import (
    DashboardBackfillRead,
    DashboardBackfillRequest,
    DashboardSnapshotRequest,
    DashboardSummaryRead,
    PortfolioSnapshotRead,
)
from app.services.dashboard import DashboardService

router = APIRouter()


@router.get("/portfolios/{portfolio_id}/summary", response_model=DashboardSummaryRead)
def get_summary(
    portfolio_id: UUID,
    run_id: UUID | None = None,
    as_of: datetime | None = None,
    session: Session = Depends(get_session),
) -> DashboardSummaryRead:
    try:
        return DashboardService(session).summary(portfolio_id, run_id=run_id, as_of=as_of)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/portfolios/{portfolio_id}/snapshots",
    response_model=PortfolioSnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def create_snapshot(
    portfolio_id: UUID,
    payload: DashboardSnapshotRequest,
    session: Session = Depends(get_session),
) -> PortfolioSnapshot:
    try:
        return DashboardService(session).capture_snapshot(
            portfolio_id,
            payload.as_of or datetime.now(timezone.utc),
            method=payload.method,
            recalculate_cost=payload.recalculate_cost,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/portfolios/{portfolio_id}/snapshots/backfill", response_model=DashboardBackfillRead)
def backfill_snapshots(
    portfolio_id: UUID,
    payload: DashboardBackfillRequest,
    session: Session = Depends(get_session),
) -> DashboardBackfillRead:
    try:
        return DashboardService(session).backfill(
            portfolio_id,
            payload.history_start,
            payload.history_end,
            method=payload.method,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/portfolios/{portfolio_id}/snapshots", response_model=list[PortfolioSnapshotRead])
def list_snapshots(
    portfolio_id: UUID,
    history_start: datetime | None = None,
    history_end: datetime | None = None,
    limit: int = Query(default=365, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> list[PortfolioSnapshot]:
    statement = select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == portfolio_id)
    if history_start:
        statement = statement.where(PortfolioSnapshot.as_of >= history_start)
    if history_end:
        statement = statement.where(PortfolioSnapshot.as_of <= history_end)
    return list(session.scalars(statement.order_by(PortfolioSnapshot.as_of.desc()).limit(limit)))
