from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.connectors.evm.chains import CHAINS
from app.connectors.evm.sync import EvmWalletSyncService
from app.core.config import get_settings
from app.db import get_session
from app.models import Account, AccountKind, EvmTrackedContract, WalletSyncRun
from app.schemas import (
    EvmChainRead,
    EvmSyncRequest,
    EvmTrackedContractBulkCreate,
    EvmTrackedContractRead,
    EvmTrackedContractUpdate,
    WalletSyncRunRead,
)
from app.services.security import add_security_event

router = APIRouter()


def _evm_account(account_id: UUID, session: Session) -> Account:
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    if account.kind != AccountKind.WALLET or account.provider != "evm":
        raise HTTPException(status_code=422, detail="tracked contracts require an EVM wallet account")
    return account


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


@router.get("/accounts/{account_id}/tracked-contracts", response_model=list[EvmTrackedContractRead])
def list_tracked_contracts(
    account_id: UUID,
    session: Session = Depends(get_session),
) -> list[EvmTrackedContract]:
    _evm_account(account_id, session)
    return list(
        session.scalars(
            select(EvmTrackedContract)
            .where(EvmTrackedContract.account_id == account_id)
            .order_by(EvmTrackedContract.is_active.desc(), EvmTrackedContract.created_at)
        )
    )


@router.post(
    "/accounts/{account_id}/tracked-contracts",
    response_model=list[EvmTrackedContractRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def add_tracked_contracts(
    account_id: UUID,
    payload: EvmTrackedContractBulkCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> list[EvmTrackedContract]:
    _evm_account(account_id, session)
    existing = {
        item.contract_address: item
        for item in session.scalars(select(EvmTrackedContract).where(EvmTrackedContract.account_id == account_id))
    }
    results: list[EvmTrackedContract] = []
    for item in payload.contracts:
        tracked = existing.get(item.contract_address)
        if tracked:
            tracked.label = item.label.strip() if item.label else tracked.label
            tracked.is_active = True
        else:
            tracked = EvmTrackedContract(
                account_id=account_id,
                contract_address=item.contract_address,
                label=item.label.strip() if item.label else None,
            )
            session.add(tracked)
            existing[item.contract_address] = tracked
        results.append(tracked)
    add_security_event(
        session,
        request,
        "evm_tracked_contracts_added",
        request.state.user.id,
        {"account_id": str(account_id), "count": len(results)},
    )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="tracked contract already exists") from error
    for item in results:
        session.refresh(item)
    return results


@router.patch(
    "/accounts/{account_id}/tracked-contracts/{contract_id}",
    response_model=EvmTrackedContractRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def update_tracked_contract(
    account_id: UUID,
    contract_id: UUID,
    payload: EvmTrackedContractUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> EvmTrackedContract:
    _evm_account(account_id, session)
    tracked = session.get(EvmTrackedContract, contract_id)
    if not tracked or tracked.account_id != account_id:
        raise HTTPException(status_code=404, detail="tracked contract not found")
    fields: list[str] = []
    if "label" in payload.model_fields_set:
        tracked.label = payload.label.strip() if payload.label else None
        fields.append("label")
    if payload.is_active is not None:
        tracked.is_active = payload.is_active
        fields.append("is_active")
    add_security_event(
        session,
        request,
        "evm_tracked_contract_updated",
        request.state.user.id,
        {"account_id": str(account_id), "contract_id": str(contract_id), "fields": fields},
    )
    session.commit()
    session.refresh(tracked)
    return tracked
