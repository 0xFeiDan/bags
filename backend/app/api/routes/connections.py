import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recent_sensitive_auth
from app.core.config import get_settings
from app.db import get_session
from app.models import Account, ApiConnection
from app.schemas import ConnectionCreate, ConnectionRead, ConnectionUpdate
from app.services.crypto import CredentialCipher, EncryptionNotConfigured
from app.services.security import add_security_event

router = APIRouter()
HYPERLIQUID_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
SUPPORTED_PROVIDERS = {"binance", "bybit", "bitget", "hyperliquid"}


def serialize(connection: ApiConnection) -> ConnectionRead:
    return ConnectionRead(
        id=connection.id,
        account_id=connection.account_id,
        name=connection.name,
        provider=connection.provider,
        api_key_hint="••••",  # Key contents are never queried again to make a hint.
        requested_permissions=connection.requested_permissions,
        is_enabled=connection.is_enabled,
        created_at=connection.created_at,
    )


@router.post(
    "",
    response_model=ConnectionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def create_connection(payload: ConnectionCreate, request: Request, session: Session = Depends(get_session)) -> ConnectionRead:
    account = session.get(Account, payload.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    provider = payload.provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS or provider != account.provider.strip().lower():
        raise HTTPException(status_code=422, detail="connection provider must match a supported account provider")
    public_identifier = account.address or account.external_account_id
    credential = payload.api_key
    if provider == "hyperliquid":
        if payload.api_secret or payload.passphrase:
            raise HTTPException(status_code=422, detail="Hyperliquid accepts a public wallet address only; never submit a private key")
        credential = credential or public_identifier
        if not credential or not HYPERLIQUID_ADDRESS.fullmatch(credential.strip()):
            raise HTTPException(status_code=422, detail="Hyperliquid accepts a public 42-character wallet address only")
        if public_identifier and public_identifier.strip().lower() != credential.strip().lower():
            raise HTTPException(status_code=422, detail="Hyperliquid connection address must match the account address")
        credential = credential.strip().lower()
    if not credential:
        raise HTTPException(status_code=422, detail="api_key is required for this provider")
    if provider in {"bybit", "bitget"} and not payload.api_secret:
        raise HTTPException(status_code=422, detail=f"{provider.title()} API secret is required")
    if provider == "bitget" and not payload.passphrase:
        raise HTTPException(status_code=422, detail="Bitget passphrase is required")
    try:
        cipher = CredentialCipher(get_settings().master_encryption_key)
    except EncryptionNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="credential encryption is not configured") from error
    connection = ApiConnection(
        account_id=payload.account_id,
        name=payload.name.strip(),
        provider=provider,
        encrypted_api_key=cipher.encrypt(credential),
        encrypted_api_secret=cipher.encrypt(payload.api_secret) if payload.api_secret else None,
        encrypted_passphrase=cipher.encrypt(payload.passphrase) if payload.passphrase else None,
        requested_permissions=payload.requested_permissions,
    )
    session.add(connection)
    try:
        add_security_event(
            session,
            request,
            "api_connection_created",
            request.state.user.id,
            {"provider": provider, "account_id": str(payload.account_id)},
        )
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="connection name already exists on this account") from error
    session.refresh(connection)
    return serialize(connection)


@router.get("", response_model=list[ConnectionRead])
def list_connections(session: Session = Depends(get_session)) -> list[ConnectionRead]:
    return [serialize(item) for item in session.scalars(select(ApiConnection).order_by(ApiConnection.created_at.desc()))]


@router.patch(
    "/{connection_id}",
    response_model=ConnectionRead,
    dependencies=[Depends(require_recent_sensitive_auth)],
)
def update_connection(
    connection_id: UUID,
    payload: ConnectionUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> ConnectionRead:
    connection = session.get(ApiConnection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="connection not found")
    account = session.get(Account, connection.account_id)
    if not account:
        raise HTTPException(status_code=409, detail="connection account is unavailable")

    provider = connection.provider.strip().lower()
    if provider == "hyperliquid":
        if payload.api_secret is not None or payload.passphrase is not None:
            raise HTTPException(status_code=422, detail="Hyperliquid accepts a public wallet address only")
        if payload.api_key is not None:
            public_identifier = account.address or account.external_account_id
            if not HYPERLIQUID_ADDRESS.fullmatch(payload.api_key.strip()):
                raise HTTPException(status_code=422, detail="Hyperliquid accepts a public 42-character wallet address only")
            if public_identifier and public_identifier.strip().lower() != payload.api_key.strip().lower():
                raise HTTPException(status_code=422, detail="Hyperliquid connection address must match the account address")

    try:
        cipher = CredentialCipher(get_settings().master_encryption_key)
    except EncryptionNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="credential encryption is not configured") from error

    updated_fields: list[str] = []
    if payload.name is not None:
        connection.name = payload.name.strip()
        updated_fields.append("name")
    if payload.api_key is not None:
        normalized_key = payload.api_key.strip().lower() if provider == "hyperliquid" else payload.api_key
        connection.encrypted_api_key = cipher.encrypt(normalized_key)
        updated_fields.append("api_key")
    if payload.api_secret is not None:
        connection.encrypted_api_secret = cipher.encrypt(payload.api_secret)
        updated_fields.append("api_secret")
    if payload.passphrase is not None:
        connection.encrypted_passphrase = cipher.encrypt(payload.passphrase)
        updated_fields.append("passphrase")
    if payload.is_enabled is not None:
        connection.is_enabled = payload.is_enabled
        updated_fields.append("is_enabled")

    try:
        add_security_event(
            session,
            request,
            "api_connection_updated",
            request.state.user.id,
            {"provider": provider, "connection_id": str(connection.id), "fields": updated_fields},
        )
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="connection name already exists on this account") from error
    session.refresh(connection)
    return serialize(connection)
from app.api.dependencies import require_recent_sensitive_auth
