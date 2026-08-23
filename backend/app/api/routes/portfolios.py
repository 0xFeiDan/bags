from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.db import get_session
from app.models import Portfolio
from app.schemas import PortfolioCreate, PortfolioRead

router = APIRouter()


@router.post("", response_model=PortfolioRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_recent_sensitive_auth)])
def create_portfolio(payload: PortfolioCreate, session: Session = Depends(get_session)) -> Portfolio:
    portfolio = Portfolio(name=payload.name.strip(), base_currency=payload.base_currency.upper())
    session.add(portfolio)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="portfolio name already exists") from error
    session.refresh(portfolio)
    return portfolio


@router.get("", response_model=list[PortfolioRead])
def list_portfolios(session: Session = Depends(get_session)) -> list[Portfolio]:
    return list(session.scalars(select(Portfolio).order_by(Portfolio.created_at.desc())))
