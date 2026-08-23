import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.db import get_session
from app.connectors.evm.chains import resolve_chain
from app.models import Account, AccountKind, Asset, BalanceSnapshot, Portfolio
from app.schemas import AccountCreate, AccountRead, BalanceSnapshotCreate, BalanceSnapshotRead

router = APIRouter()
EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_recent_sensitive_auth)])
def create_account(payload: AccountCreate, session: Session = Depends(get_session)) -> Account:
    if not session.get(Portfolio, payload.portfolio_id):
        raise HTTPException(status_code=404, detail="portfolio not found")
    normalized_provider = payload.provider.strip().lower()
    account_data = payload.model_dump()
    account_data["provider"] = normalized_provider
    if payload.kind == AccountKind.WALLET and normalized_provider == "evm":
        chain = resolve_chain(payload.chain_id)
        if not chain:
            raise HTTPException(status_code=422, detail="unsupported EVM chain")
        address = (payload.address or payload.external_account_id or "").strip().lower()
        if not EVM_ADDRESS.fullmatch(address):
            raise HTTPException(status_code=422, detail="EVM wallet requires a valid public 42-character address")
        if payload.address and payload.external_account_id and payload.address.lower() != payload.external_account_id.lower():
            raise HTTPException(status_code=422, detail="wallet address and external account ID must match")
        account_data["chain_id"] = chain.chain_id
        account_data["address"] = address
        account_data["external_account_id"] = f"{chain.chain_id}:{address}"
    identity = account_data.get("external_account_id") or account_data.get("address")
    if identity:
        duplicate = session.scalar(
            select(Account.id).where(
                Account.portfolio_id == payload.portfolio_id,
                Account.provider == normalized_provider,
                (Account.external_account_id == identity) | (Account.address == identity),
            )
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="account source identity already exists")
    account = Account(**account_data)
    session.add(account)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="account source identity already exists") from error
    session.refresh(account)
    return account


@router.get("", response_model=list[AccountRead])
def list_accounts(
    portfolio_id: UUID | None = Query(default=None), session: Session = Depends(get_session)
) -> list[Account]:
    statement = select(Account).order_by(Account.provider, Account.label)
    if portfolio_id:
        statement = statement.where(Account.portfolio_id == portfolio_id)
    return list(session.scalars(statement))


@router.post(
    "/{account_id}/balance-snapshots",
    response_model=BalanceSnapshotRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def create_balance_snapshot(
    account_id: UUID, payload: BalanceSnapshotCreate, session: Session = Depends(get_session)
) -> BalanceSnapshot:
    if payload.account_id != account_id:
        raise HTTPException(status_code=422, detail="account_id must match URL")
    if not session.get(Account, payload.account_id):
        raise HTTPException(status_code=404, detail="account not found")
    if not session.get(Asset, payload.asset_id):
        raise HTTPException(status_code=404, detail="asset not found")
    snapshot = BalanceSnapshot(**payload.model_dump())
    session.add(snapshot)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="balance snapshot already exists for this timestamp") from error
    session.refresh(snapshot)
    return snapshot


@router.get("/{account_id}/balance-snapshots", response_model=list[BalanceSnapshotRead])
def list_balance_snapshots(account_id: UUID, session: Session = Depends(get_session)) -> list[BalanceSnapshot]:
    return list(
        session.scalars(
            select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id).order_by(BalanceSnapshot.as_of.desc())
        )
    )
