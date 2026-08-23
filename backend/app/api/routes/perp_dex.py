from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.perp_dex.hyperliquid.sync import HyperliquidSyncService
from app.core.config import get_settings
from app.db import get_session
from app.models import AccountEquitySnapshot, ApiConnection, PositionSnapshot, SyncRun
from app.schemas import AccountEquitySnapshotRead, HyperliquidSyncRequest, PositionSnapshotRead, SyncRunRead

router = APIRouter()


@router.post("/connections/{connection_id}/sync", response_model=SyncRunRead)
def sync_perp_dex(
    connection_id: UUID,
    payload: HyperliquidSyncRequest,
    session: Session = Depends(get_session),
) -> SyncRun:
    connection = session.get(ApiConnection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    if connection.provider != "hyperliquid":
        raise HTTPException(status_code=422, detail="Phase 3 supports Hyperliquid connections")
    try:
        return HyperliquidSyncService(session, get_settings()).run(connection_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/connections/{connection_id}/sync-runs", response_model=list[SyncRunRead])
def list_sync_runs(
    connection_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[SyncRun]:
    return list(
        session.scalars(
            select(SyncRun)
            .where(SyncRun.connection_id == connection_id)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        )
    )


@router.get("/accounts/{account_id}/equity", response_model=list[AccountEquitySnapshotRead])
def list_equity_snapshots(
    account_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[AccountEquitySnapshot]:
    return list(
        session.scalars(
            select(AccountEquitySnapshot)
            .where(AccountEquitySnapshot.account_id == account_id)
            .order_by(AccountEquitySnapshot.as_of.desc())
            .limit(limit)
        )
    )


@router.get("/accounts/{account_id}/positions", response_model=list[PositionSnapshotRead])
def list_positions(
    account_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[PositionSnapshot]:
    return list(
        session.scalars(
            select(PositionSnapshot)
            .where(
                PositionSnapshot.account_id == account_id,
                PositionSnapshot.product == "hyperliquid_perp",
            )
            .order_by(PositionSnapshot.as_of.desc())
            .limit(limit)
        )
    )
