from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.db import get_session
from app.models import Account, ApiConnection, Asset, LedgerEntry, LedgerEvent, Portfolio, RawEvent
from app.schemas import LedgerEntryRead, LedgerEventCreate, LedgerEventRead

router = APIRouter()


def serialize(event: LedgerEvent, session: Session) -> LedgerEventRead:
    entries = list(session.scalars(select(LedgerEntry).where(LedgerEntry.ledger_event_id == event.id)))
    return LedgerEventRead(
        id=event.id,
        portfolio_id=event.portfolio_id,
        raw_event_id=event.raw_event_id,
        event_type=event.event_type,
        source=event.source,
        status=event.status,
        occurred_at=event.occurred_at,
        tx_hash=event.tx_hash,
        external_reference=event.external_reference,
        note=event.note,
        metadata_json=event.metadata_json,
        created_at=event.created_at,
        entries=[LedgerEntryRead.model_validate(entry) for entry in entries],
    )


@router.post(
    "/events",
    response_model=LedgerEventRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def create_ledger_event(payload: LedgerEventCreate, session: Session = Depends(get_session)) -> LedgerEventRead:
    if not session.get(Portfolio, payload.portfolio_id):
        raise HTTPException(status_code=404, detail="portfolio not found")
    raw_event = session.get(RawEvent, payload.raw_event_id) if payload.raw_event_id else None
    if payload.raw_event_id and not raw_event:
        raise HTTPException(status_code=404, detail="raw event not found")
    account_ids = {entry.account_id for entry in payload.entries}
    asset_ids = {entry.asset_id for entry in payload.entries}
    accounts = {account.id: account for account in session.scalars(select(Account).where(Account.id.in_(account_ids)))}
    known_accounts = set(accounts)
    known_assets = set(session.scalars(select(Asset.id).where(Asset.id.in_(asset_ids))))
    if known_accounts != account_ids:
        raise HTTPException(status_code=422, detail="one or more ledger entry accounts do not exist")
    if known_assets != asset_ids:
        raise HTTPException(status_code=422, detail="one or more ledger entry assets do not exist")
    if any(account.portfolio_id != payload.portfolio_id for account in accounts.values()):
        raise HTTPException(status_code=422, detail="every ledger entry account must belong to the event portfolio")
    if raw_event:
        if not raw_event.account_id or raw_event.account_id not in account_ids:
            raise HTTPException(status_code=422, detail="raw event must belong to one of the ledger entry accounts")
        raw_account = session.get(Account, raw_event.account_id)
        if not raw_account or raw_account.portfolio_id != payload.portfolio_id:
            raise HTTPException(status_code=422, detail="raw event account must belong to the event portfolio")
        if raw_event.connection_id:
            connection = session.get(ApiConnection, raw_event.connection_id)
            if not connection or connection.account_id != raw_event.account_id:
                raise HTTPException(status_code=422, detail="raw event connection must match its account")

    event = LedgerEvent(**payload.model_dump(exclude={"entries"}))
    session.add(event)
    session.flush()
    session.add_all([LedgerEntry(ledger_event_id=event.id, **entry.model_dump()) for entry in payload.entries])
    session.commit()
    session.refresh(event)
    return serialize(event, session)


@router.get("/events", response_model=list[LedgerEventRead])
def list_ledger_events(
    portfolio_id: UUID | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500), session: Session = Depends(get_session)
) -> list[LedgerEventRead]:
    statement = select(LedgerEvent).order_by(LedgerEvent.occurred_at.desc()).limit(limit)
    if portfolio_id:
        statement = statement.where(LedgerEvent.portfolio_id == portfolio_id)
    return [serialize(event, session) for event in session.scalars(statement)]
