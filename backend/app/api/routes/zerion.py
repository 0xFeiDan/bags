from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.connectors.evm.chains import resolve_chain
from app.connectors.zerion.sync import ZerionShadowSyncService, ZerionSyncRejected
from app.core.config import Settings, get_settings
from app.db import get_session
from app.models import Account, AccountDataSource, AccountKind, DataSourceMode, ProviderSyncRun
from app.schemas import (
    ProviderSyncRunRead,
    ZerionDataSourceRead,
    ZerionDataSourceUpsert,
    ZerionShadowSyncRequest,
    ZerionStatusRead,
)
from app.services.security import add_security_event

router = APIRouter()
ZERION_PROVIDER = "zerion"
FREE_REQUESTS_PER_SECOND_LIMIT = 1
FREE_DAILY_REQUEST_LIMIT = 300
FREE_MAX_REQUESTS_PER_RUN = 3
FREE_MIN_SYNC_INTERVAL_SECONDS = 900
FREE_DAILY_REQUEST_BUDGET = 270


def _wallet_account(session: Session, account_id: UUID) -> Account:
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    if account.kind != AccountKind.WALLET or account.provider != "evm" or not resolve_chain(account.chain_id):
        raise HTTPException(status_code=422, detail="Zerion Phase 1 supports configured EVM wallet accounts only")
    return account


def _serialize(source: AccountDataSource, settings: Settings) -> ZerionDataSourceRead:
    return ZerionDataSourceRead(
        id=source.id,
        account_id=source.account_id,
        provider=source.provider,
        mode=source.mode,
        is_enabled=source.is_enabled,
        requests_per_second_limit=source.requests_per_second_limit,
        daily_request_limit=source.daily_request_limit,
        max_requests_per_run=source.max_requests_per_run,
        min_sync_interval_seconds=source.min_sync_interval_seconds,
        daily_request_budget=source.daily_request_budget,
        remote_subscription_id=source.remote_subscription_id,
        cursor_value=source.cursor_value,
        last_synced_at=source.last_synced_at,
        next_sync_after=source.next_sync_after,
        zerion_configured=bool(settings.zerion_enabled and settings.zerion_api_key),
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


@router.get("/status", response_model=ZerionStatusRead)
def get_status() -> ZerionStatusRead:
    settings = get_settings()
    daily_limit = max(1, min(settings.zerion_daily_request_limit, FREE_DAILY_REQUEST_LIMIT))
    return ZerionStatusRead(
        configured=bool(settings.zerion_enabled and settings.zerion_api_key),
        requests_per_second_limit=max(
            1,
            min(settings.zerion_requests_per_second_limit, FREE_REQUESTS_PER_SECOND_LIMIT),
        ),
        daily_request_limit=daily_limit,
        daily_request_budget=max(
            1,
            min(settings.zerion_daily_request_budget, daily_limit, FREE_DAILY_REQUEST_BUDGET),
        ),
        max_requests_per_run=max(1, min(settings.zerion_max_requests_per_run, FREE_MAX_REQUESTS_PER_RUN)),
        min_sync_interval_seconds=max(settings.zerion_min_sync_interval_seconds, FREE_MIN_SYNC_INTERVAL_SECONDS),
    )


@router.put(
    "/accounts/{account_id}/source",
    response_model=ZerionDataSourceRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def configure_source(
    account_id: UUID,
    payload: ZerionDataSourceUpsert,
    request: Request,
    session: Session = Depends(get_session),
) -> ZerionDataSourceRead:
    _wallet_account(session, account_id)
    settings = get_settings()
    if payload.is_enabled and not (settings.zerion_enabled and settings.zerion_api_key):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Zerion is not configured on the server")

    source = session.scalar(
        select(AccountDataSource).where(
            AccountDataSource.account_id == account_id,
            AccountDataSource.provider == ZERION_PROVIDER,
        )
    )
    if not source:
        requests_per_second_limit = max(
            1,
            min(settings.zerion_requests_per_second_limit, FREE_REQUESTS_PER_SECOND_LIMIT),
        )
        daily_request_limit = max(1, min(settings.zerion_daily_request_limit, FREE_DAILY_REQUEST_LIMIT))
        source = AccountDataSource(
            account_id=account_id,
            provider=ZERION_PROVIDER,
            # Environment settings may make the free-plan guard tighter, never
            # looser. Phase 2 will enforce these persisted caps before a call.
            requests_per_second_limit=requests_per_second_limit,
            daily_request_limit=daily_request_limit,
            max_requests_per_run=max(1, min(settings.zerion_max_requests_per_run, FREE_MAX_REQUESTS_PER_RUN)),
            min_sync_interval_seconds=max(settings.zerion_min_sync_interval_seconds, FREE_MIN_SYNC_INTERVAL_SECONDS),
            daily_request_budget=max(
                1,
                min(settings.zerion_daily_request_budget, daily_request_limit, FREE_DAILY_REQUEST_BUDGET),
            ),
        )
        session.add(source)

    source.mode = payload.mode
    source.is_enabled = payload.is_enabled
    add_security_event(
        session,
        request,
        "zerion_data_source_configured",
        request.state.user.id,
        {"account_id": str(account_id), "mode": payload.mode.value, "is_enabled": payload.is_enabled},
    )
    session.commit()
    session.refresh(source)
    return _serialize(source, settings)


@router.get("/accounts/{account_id}/source", response_model=ZerionDataSourceRead)
def get_source(account_id: UUID, session: Session = Depends(get_session)) -> ZerionDataSourceRead:
    _wallet_account(session, account_id)
    source = session.scalar(
        select(AccountDataSource).where(
            AccountDataSource.account_id == account_id,
            AccountDataSource.provider == ZERION_PROVIDER,
        )
    )
    if not source:
        raise HTTPException(status_code=404, detail="Zerion data source is not configured for this account")
    return _serialize(source, get_settings())


@router.get("/accounts/{account_id}/sync-runs", response_model=list[ProviderSyncRunRead])
def list_sync_runs(
    account_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[ProviderSyncRun]:
    _wallet_account(session, account_id)
    return list(
        session.scalars(
            select(ProviderSyncRun)
            .join(AccountDataSource, ProviderSyncRun.data_source_id == AccountDataSource.id)
            .where(
                AccountDataSource.account_id == account_id,
                AccountDataSource.provider == ZERION_PROVIDER,
            )
            .order_by(ProviderSyncRun.started_at.desc())
            .limit(limit)
        )
    )


@router.post(
    "/accounts/{account_id}/shadow-sync",
    response_model=ProviderSyncRunRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def run_shadow_sync(
    account_id: UUID,
    _payload: ZerionShadowSyncRequest,
    session: Session = Depends(get_session),
) -> ProviderSyncRun:
    try:
        return ZerionShadowSyncService(session, get_settings()).run(account_id)
    except ZerionSyncRejected as error:
        headers = {"Retry-After": str(error.retry_after_seconds)} if error.retry_after_seconds else None
        status_code = status.HTTP_429_TOO_MANY_REQUESTS if error.retry_after_seconds else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=status_code, detail=str(error), headers=headers) from error
