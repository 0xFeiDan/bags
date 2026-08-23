import hashlib
import json

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.db import get_session
from app.models import Account, ApiConnection, RawEvent
from app.schemas import RawEventCreate, RawEventRead

router = APIRouter()


def fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.post("", response_model=RawEventRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_recent_sensitive_auth)])
def ingest_raw_event(payload: RawEventCreate, session: Session = Depends(get_session)) -> RawEvent:
    account = session.get(Account, payload.account_id) if payload.account_id else None
    if payload.account_id and not account:
        raise HTTPException(status_code=404, detail="account not found")
    connection = session.get(ApiConnection, payload.connection_id) if payload.connection_id else None
    if payload.connection_id and not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    if connection and account and connection.account_id != account.id:
        raise HTTPException(status_code=422, detail="connection must belong to the raw event account")
    values = payload.model_dump()
    if connection and not account:
        values["account_id"] = connection.account_id
    raw_event = RawEvent(**values, payload_hash=fingerprint(payload.payload_json))
    session.add(raw_event)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="raw event already exists for this source") from error
    session.refresh(raw_event)
    return raw_event


@router.get("", response_model=list[RawEventRead])
def list_raw_events(
    account_id: UUID | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500), session: Session = Depends(get_session)
) -> list[RawEvent]:
    statement = select(RawEvent).order_by(RawEvent.occurred_at.desc()).limit(limit)
    if account_id:
        statement = statement.where(RawEvent.account_id == account_id)
    return list(session.scalars(statement))
