from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.bitget.sync import BitgetSyncService
from app.connectors.bybit.sync import BybitSyncService
from app.core.config import get_settings
from app.db import get_session
from app.models import ApiConnection, SyncRun
from app.schemas import BitgetSyncRequest, BybitSyncRequest, SyncRunRead
from app.services.crypto import EncryptionNotConfigured

router = APIRouter()


@router.post("/bybit/connections/{connection_id}/sync", response_model=SyncRunRead)
def sync_bybit(connection_id: UUID, payload: BybitSyncRequest, session: Session = Depends(get_session)) -> SyncRun:
    connection = session.get(ApiConnection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    if connection.provider != "bybit":
        raise HTTPException(status_code=422, detail="connection is not a Bybit connection")
    try:
        return BybitSyncService(session, get_settings()).run(connection_id, payload)
    except EncryptionNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="credential encryption is not configured") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/bitget/connections/{connection_id}/sync", response_model=SyncRunRead)
def sync_bitget(connection_id: UUID, payload: BitgetSyncRequest, session: Session = Depends(get_session)) -> SyncRun:
    connection = session.get(ApiConnection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    if connection.provider != "bitget":
        raise HTTPException(status_code=422, detail="connection is not a Bitget connection")
    try:
        return BitgetSyncService(session, get_settings()).run(connection_id, payload)
    except EncryptionNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="credential encryption is not configured") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/{provider}/connections/{connection_id}/sync-runs", response_model=list[SyncRunRead])
def list_exchange_sync_runs(
    provider: str,
    connection_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[SyncRun]:
    if provider not in {"bybit", "bitget"}:
        raise HTTPException(status_code=404, detail="exchange connector not found")
    connection = session.get(ApiConnection, connection_id)
    if not connection or connection.provider != provider:
        raise HTTPException(status_code=404, detail="connection not found")
    return list(session.scalars(select(SyncRun).where(SyncRun.connection_id == connection_id).order_by(SyncRun.started_at.desc()).limit(limit)))
