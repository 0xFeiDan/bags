from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.core.config import get_settings
from app.db import get_session
from app.models import (
    TransferCandidate,
    TransferCandidateStatus,
    TransferGroup,
    TransferGroupStatus,
    TransferMatchRun,
)
from app.schemas import (
    ManualTransferMatchRequest,
    TransferActionRequest,
    TransferCandidateRead,
    TransferGroupRead,
    TransferMatchRequest,
    TransferMatchRunRead,
)
from app.services.security import add_security_event
from app.services.transfer_matching import TransferMatchingService

router = APIRouter()


def service(session: Session) -> TransferMatchingService:
    return TransferMatchingService(session, get_settings())


@router.post("/portfolios/{portfolio_id}/match", response_model=TransferMatchRunRead)
def match_transfers(
    portfolio_id: UUID,
    payload: TransferMatchRequest,
    session: Session = Depends(get_session),
) -> TransferMatchRun:
    try:
        return service(session).run(portfolio_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/portfolios/{portfolio_id}/runs", response_model=list[TransferMatchRunRead])
def list_runs(
    portfolio_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[TransferMatchRun]:
    return list(
        session.scalars(
            select(TransferMatchRun)
            .where(TransferMatchRun.portfolio_id == portfolio_id)
            .order_by(TransferMatchRun.started_at.desc())
            .limit(limit)
        )
    )


@router.get("/portfolios/{portfolio_id}/candidates", response_model=list[TransferCandidateRead])
def list_candidates(
    portfolio_id: UUID,
    candidate_status: TransferCandidateStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[TransferCandidate]:
    statement = (
        select(TransferCandidate)
        .where(TransferCandidate.portfolio_id == portfolio_id)
        .order_by(TransferCandidate.score.desc(), TransferCandidate.updated_at.desc())
        .limit(limit)
    )
    if candidate_status:
        statement = statement.where(TransferCandidate.status == candidate_status)
    return list(session.scalars(statement))


@router.get("/portfolios/{portfolio_id}/groups", response_model=list[TransferGroupRead])
def list_groups(
    portfolio_id: UUID,
    group_status: TransferGroupStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[TransferGroup]:
    statement = (
        select(TransferGroup)
        .where(TransferGroup.portfolio_id == portfolio_id)
        .order_by(TransferGroup.created_at.desc())
        .limit(limit)
    )
    if group_status:
        statement = statement.where(TransferGroup.status == group_status)
    return list(session.scalars(statement))


@router.post(
    "/candidates/{candidate_id}/confirm",
    response_model=TransferGroupRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def confirm_candidate(
    candidate_id: UUID,
    payload: TransferActionRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> TransferGroup:
    try:
        group = service(session).confirm_candidate(candidate_id, payload.note)
        add_security_event(session, request, "transfer_match_confirmed", request.state.user.id, {"group_id": str(group.id)})
        session.commit()
        return group
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/manual",
    response_model=TransferGroupRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def manual_match(
    payload: ManualTransferMatchRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> TransferGroup:
    try:
        group = service(session).manual_match(payload.source_event_id, payload.destination_event_id, payload.note)
        add_security_event(session, request, "transfer_manual_match", request.state.user.id, {"group_id": str(group.id)})
        session.commit()
        return group
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/groups/{group_id}/unmatch",
    response_model=TransferGroupRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def unmatch_group(
    group_id: UUID,
    payload: TransferActionRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> TransferGroup:
    try:
        group = service(session).unmatch(group_id, payload.note)
        add_security_event(session, request, "transfer_group_unmatched", request.state.user.id, {"group_id": str(group.id)})
        session.commit()
        return group
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/candidates/{candidate_id}/ignore",
    response_model=TransferCandidateRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def ignore_candidate(
    candidate_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
) -> TransferCandidate:
    try:
        candidate = service(session).ignore_candidate(candidate_id)
        add_security_event(session, request, "transfer_candidate_ignored", request.state.user.id, {"candidate_id": str(candidate.id)})
        session.commit()
        return candidate
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
