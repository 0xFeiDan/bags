from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.db import get_session
from app.models import Asset
from app.schemas import AssetCreate, AssetRead

router = APIRouter()


@router.post("", response_model=AssetRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_recent_sensitive_auth)])
def create_asset(payload: AssetCreate, session: Session = Depends(get_session)) -> Asset:
    if payload.underlying_asset_id and not session.get(Asset, payload.underlying_asset_id):
        raise HTTPException(status_code=404, detail="underlying asset not found")
    chain_id = payload.chain_id.strip() if payload.chain_id else None
    contract_address = payload.contract_address.lower() if payload.contract_address else None
    existing = session.scalar(
        select(Asset).where(
            Asset.canonical_symbol == payload.canonical_symbol.upper(),
            Asset.chain_id.is_(None) if chain_id is None else Asset.chain_id == chain_id,
            Asset.contract_address.is_(None) if contract_address is None else Asset.contract_address == contract_address,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="asset identity already exists")
    asset = Asset(
        canonical_symbol=payload.canonical_symbol.upper(),
        name=payload.name.strip(),
        asset_type=payload.asset_type,
        decimals=payload.decimals,
        chain_id=chain_id,
        contract_address=contract_address,
        underlying_asset_id=payload.underlying_asset_id,
    )
    session.add(asset)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="asset identity already exists") from error
    session.refresh(asset)
    return asset


@router.get("", response_model=list[AssetRead])
def list_assets(
    symbol: str | None = Query(default=None, max_length=32), session: Session = Depends(get_session)
) -> list[Asset]:
    statement = select(Asset).order_by(Asset.canonical_symbol)
    if symbol:
        statement = statement.where(Asset.canonical_symbol == symbol.upper())
    return list(session.scalars(statement))
