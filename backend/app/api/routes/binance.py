from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.binance.sync import BinanceSyncService
from app.core.config import get_settings
from app.db import get_session
from app.models import ApiConnection, PositionSnapshot, SyncRun
from app.schemas import BinanceSyncRead, BinanceSyncRequest, PositionSnapshotRead
from app.services.crypto import EncryptionNotConfigured

router = APIRouter()


@router.post("/connections/{connection_id}/sync", response_model=BinanceSyncRead)
def sync_binance(
    connection_id: UUID,
    payload: BinanceSyncRequest,
    session: Session = Depends(get_session),
) -> SyncRun:
    connection = session.get(ApiConnection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    if connection.provider != "binance":
        raise HTTPException(status_code=422, detail="connection is not a Binance connection")
    try:
        return BinanceSyncService(session, get_settings()).run(connection_id, payload)
    except EncryptionNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="credential encryption is not configured") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/connections/{connection_id}/sync-runs", response_model=list[BinanceSyncRead])
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


@router.get("/accounts/{account_id}/positions", response_model=list[PositionSnapshotRead])
def list_positions(
    account_id: UUID,
    product: str | None = Query(default=None, pattern="^(usdm|coinm)$"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[PositionSnapshot]:
    statement = (
        select(PositionSnapshot)
        .where(PositionSnapshot.account_id == account_id)
        .order_by(PositionSnapshot.as_of.desc())
        .limit(limit)
    )
    if product:
        statement = statement.where(PositionSnapshot.product == product)
    return list(session.scalars(statement))
