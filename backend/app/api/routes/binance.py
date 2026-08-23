from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.connectors.binance.sync import BinanceSyncService
from app.core.config import get_settings
from app.db import get_session
from app.models import ApiConnection, ConnectionMarketScope, PositionSnapshot, SyncRun
from app.schemas import (
    BinanceSpotSymbolBulkCreate,
    BinanceSpotSymbolRead,
    BinanceSpotSymbolUpdate,
    BinanceSyncRead,
    BinanceSyncRequest,
    PositionSnapshotRead,
)
from app.services.crypto import EncryptionNotConfigured
from app.services.security import add_security_event

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


def _binance_connection(connection_id: UUID, session: Session) -> ApiConnection:
    connection = session.get(ApiConnection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    if connection.provider != "binance":
        raise HTTPException(status_code=422, detail="connection is not a Binance connection")
    return connection


@router.get("/connections/{connection_id}/spot-symbols", response_model=list[BinanceSpotSymbolRead])
def list_spot_symbols(
    connection_id: UUID,
    session: Session = Depends(get_session),
) -> list[ConnectionMarketScope]:
    _binance_connection(connection_id, session)
    return list(
        session.scalars(
            select(ConnectionMarketScope)
            .where(ConnectionMarketScope.connection_id == connection_id, ConnectionMarketScope.product == "spot")
            .order_by(ConnectionMarketScope.is_active.desc(), ConnectionMarketScope.symbol)
        )
    )


@router.post(
    "/connections/{connection_id}/spot-symbols",
    response_model=list[BinanceSpotSymbolRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def add_spot_symbols(
    connection_id: UUID,
    payload: BinanceSpotSymbolBulkCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> list[ConnectionMarketScope]:
    _binance_connection(connection_id, session)
    existing = {
        item.symbol: item
        for item in session.scalars(
            select(ConnectionMarketScope).where(
                ConnectionMarketScope.connection_id == connection_id,
                ConnectionMarketScope.product == "spot",
            )
        )
    }
    results: list[ConnectionMarketScope] = []
    for symbol in payload.symbols:
        scope = existing.get(symbol)
        if scope:
            scope.is_active = True
            scope.discovery_source = "manual"
        else:
            scope = ConnectionMarketScope(
                connection_id=connection_id,
                product="spot",
                symbol=symbol,
                discovery_source="manual",
            )
            session.add(scope)
            existing[symbol] = scope
        results.append(scope)
    add_security_event(
        session,
        request,
        "binance_spot_symbols_added",
        request.state.user.id,
        {"connection_id": str(connection_id), "count": len(results)},
    )
    session.commit()
    for item in results:
        session.refresh(item)
    return results


@router.patch(
    "/connections/{connection_id}/spot-symbols/{scope_id}",
    response_model=BinanceSpotSymbolRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def update_spot_symbol(
    connection_id: UUID,
    scope_id: UUID,
    payload: BinanceSpotSymbolUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> ConnectionMarketScope:
    _binance_connection(connection_id, session)
    scope = session.get(ConnectionMarketScope, scope_id)
    if not scope or scope.connection_id != connection_id or scope.product != "spot":
        raise HTTPException(status_code=404, detail="Spot symbol scope not found")
    scope.is_active = payload.is_active
    add_security_event(
        session,
        request,
        "binance_spot_symbol_updated",
        request.state.user.id,
        {"connection_id": str(connection_id), "scope_id": str(scope_id), "is_active": payload.is_active},
    )
    session.commit()
    session.refresh(scope)
    return scope
