from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.evm.chains import CHAINS
from app.connectors.evm.sync import EvmWalletSyncService
from app.core.config import get_settings
from app.db import get_session
from app.models import WalletSyncRun
from app.schemas import EvmChainRead, EvmSyncRequest, WalletSyncRunRead

router = APIRouter()


@router.get("/chains", response_model=list[EvmChainRead])
def list_chains() -> list[EvmChainRead]:
    settings = get_settings()
    return [
        EvmChainRead(
            key=chain.key,
            chain_id=chain.chain_id,
            name=chain.name,
            native_symbol=chain.native_symbol,
            configured=bool(getattr(settings, chain.rpc_setting, None)),
        )
        for chain in CHAINS.values()
    ]


@router.post("/accounts/{account_id}/sync", response_model=WalletSyncRunRead)
def sync_wallet(
    account_id: UUID,
    payload: EvmSyncRequest,
    session: Session = Depends(get_session),
) -> WalletSyncRun:
    try:
        return EvmWalletSyncService(session, get_settings()).run(account_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/accounts/{account_id}/sync-runs", response_model=list[WalletSyncRunRead])
def list_sync_runs(
    account_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[WalletSyncRun]:
    return list(
        session.scalars(
            select(WalletSyncRun)
            .where(WalletSyncRun.account_id == account_id)
            .order_by(WalletSyncRun.started_at.desc())
            .limit(limit)
        )
    )
