import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.evm.chains import resolve_chain
from app.connectors.evm.collector import normalize_address
from app.connectors.zerion.client import ZerionApiClient, ZerionApiError, ZerionPage, ZerionRateLimitError
from app.connectors.zerion.collector import ZerionShadowCollector
from app.connectors.zerion.limits import ZerionBudgetExceeded, ZerionRequestGovernor
from app.core.config import Settings
from app.models import (
    Account,
    AccountDataSource,
    AccountKind,
    DataSourceMode,
    ProviderSyncCursor,
    ProviderSyncRun,
    RawEvent,
    RawEventStatus,
    SyncRunStatus,
    utc_now,
)

ZERION_CHAIN_IDS = {
    "ethereum": "ethereum",
    "arbitrum": "arbitrum",
    "base": "base",
    "bsc": "binance-smart-chain",
    "optimism": "optimism",
    "polygon": "polygon",
}


class ZerionSyncRejected(ValueError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ZerionShadowStats:
    def __init__(self) -> None:
        self.pages_collected = 0
        self.transactions_seen = 0
        self.positions_seen = 0
        self.raw_created = 0
        self.raw_existing = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pages_collected": self.pages_collected,
            "transactions_seen": self.transactions_seen,
            "positions_seen": self.positions_seen,
            "raw_created": self.raw_created,
            "raw_existing": self.raw_existing,
            "ledger_created": 0,
        }


class ZerionShadowSyncService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.transport = transport
        self._now = now or utc_now
        self._sleep = sleep
        self.stats = ZerionShadowStats()
        self.warnings: list[str] = []

    def run(self, account_id: UUID) -> ProviderSyncRun:
        account, source, chain_id = self._validate(account_id)
        now = self._aware(self._now())
        if source.next_sync_after and self._aware(source.next_sync_after) > now:
            retry_after = max(1, int((self._aware(source.next_sync_after) - now).total_seconds()))
            raise ZerionSyncRejected("Zerion shadow sync is cooling down", retry_after_seconds=retry_after)

        run = ProviderSyncRun(
            data_source_id=source.id,
            request_kind="shadow_manual",
            request_budget=min(source.max_requests_per_run, 3),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        try:
            cursor = self._cursor(source.id)
            governor = ZerionRequestGovernor(
                self.session,
                source,
                run,
                now=self._now,
                **({"sleep": self._sleep} if self._sleep else {}),
            )
            with ZerionApiClient(
                api_key=str(self.settings.zerion_api_key),
                base_url=self.settings.zerion_base_url,
                timeout_seconds=self.settings.zerion_request_timeout_seconds,
                governor=governor,
                transport=self.transport,
            ) as client:
                collection = ZerionShadowCollector(client).collect(
                    normalize_address(account.address or ""),
                    chain_id,
                    backfill_url=cursor.cursor_url if cursor else None,
                    backfill_complete=cursor.is_complete if cursor else False,
                    on_page=lambda resource, page: self._persist_page(account, chain_id, resource, page),
                )
            self.warnings.extend(collection.warnings)
            self._save_cursor(
                source.id,
                collection.next_backfill_url,
                collection.backfill_complete,
                self._aware(self._now()),
            )
            status = SyncRunStatus.PARTIAL if self.warnings else SyncRunStatus.SUCCEEDED
            return self._finish(run.id, source.id, status)
        except ZerionRateLimitError as error:
            self.session.rollback()
            return self._finish(
                run.id,
                source.id,
                SyncRunStatus.PARTIAL if self.stats.pages_collected else SyncRunStatus.FAILED,
                "ZERION_RATE_LIMITED",
                str(error),
            )
        except ZerionBudgetExceeded as error:
            self.session.rollback()
            return self._finish(
                run.id,
                source.id,
                SyncRunStatus.PARTIAL if self.stats.pages_collected else SyncRunStatus.FAILED,
                "ZERION_BUDGET_EXHAUSTED",
                str(error),
            )
        except ZerionApiError as error:
            self.session.rollback()
            return self._finish(
                run.id,
                source.id,
                SyncRunStatus.PARTIAL if self.stats.pages_collected else SyncRunStatus.FAILED,
                "ZERION_API_ERROR",
                str(error),
            )
        except Exception as error:
            self.session.rollback()
            return self._finish(run.id, source.id, SyncRunStatus.FAILED, "ZERION_SHADOW_ERROR", str(error)[:300])

    def _validate(self, account_id: UUID) -> tuple[Account, AccountDataSource, str]:
        if not self.settings.zerion_enabled or not self.settings.zerion_api_key:
            raise ZerionSyncRejected("Zerion is not configured on the server")
        account = self.session.get(Account, account_id)
        if not account or not account.is_active:
            raise ZerionSyncRejected("wallet account not found or disabled")
        if account.kind != AccountKind.WALLET or account.provider != "evm":
            raise ZerionSyncRejected("Zerion shadow sync requires an EVM wallet account")
        chain = resolve_chain(account.chain_id)
        if not chain or chain.key not in ZERION_CHAIN_IDS:
            raise ZerionSyncRejected("wallet chain is not supported by the Zerion shadow connector")
        address = normalize_address(account.address or "")
        if not address.startswith("0x") or len(address) != 42:
            raise ZerionSyncRejected("wallet account has no valid public EVM address")
        source = self.session.scalar(
            select(AccountDataSource).where(
                AccountDataSource.account_id == account.id,
                AccountDataSource.provider == "zerion",
            )
        )
        if not source or not source.is_enabled or source.mode != DataSourceMode.SHADOW:
            raise ZerionSyncRejected("Zerion data source is not enabled in shadow mode")
        return account, source, ZERION_CHAIN_IDS[chain.key]

    def _cursor(self, source_id: UUID) -> ProviderSyncCursor | None:
        return self.session.scalar(
            select(ProviderSyncCursor).where(
                ProviderSyncCursor.data_source_id == source_id,
                ProviderSyncCursor.resource == "transactions_backfill",
            )
        )

    def _save_cursor(self, source_id: UUID, url: str | None, complete: bool, observed_at: datetime) -> None:
        cursor = self._cursor(source_id)
        if not cursor:
            cursor = ProviderSyncCursor(data_source_id=source_id, resource="transactions_backfill")
            self.session.add(cursor)
        cursor.cursor_url = url
        cursor.is_complete = complete
        cursor.last_synced_at = observed_at
        self.session.commit()

    def _persist_page(
        self,
        account: Account,
        expected_chain: str,
        resource: str,
        page: ZerionPage,
    ) -> None:
        self.stats.pages_collected += 1
        if resource == "transactions":
            self.stats.transactions_seen += len(page.data)
            for item in page.data:
                self._persist_transaction(account, expected_chain, item)
        else:
            self.stats.positions_seen += len(page.data)
            for item in page.data:
                self._persist_position(account, expected_chain, item)
        self.session.commit()

    def _persist_transaction(self, account: Account, expected_chain: str, item: dict[str, Any]) -> None:
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        relationships = item.get("relationships") if isinstance(item.get("relationships"), dict) else {}
        chain_rel = relationships.get("chain") if isinstance(relationships.get("chain"), dict) else {}
        chain_data = chain_rel.get("data") if isinstance(chain_rel.get("data"), dict) else {}
        chain_id = str(chain_data.get("id") or expected_chain).lower()
        tx_hash = str(attributes.get("hash") or "").lower()
        payload_hash = self._payload_hash(item)
        if chain_id != expected_chain:
            self.warnings.append(f"Zerion returned transaction data for unexpected chain {chain_id}.")
        base_identity = f"{chain_id}:{tx_hash}" if tx_hash else f"{chain_id}:missing-hash:{payload_hash[:24]}"
        occurred_at = self._parse_time(attributes.get("mined_at"))
        self._raw(account.id, "zerion:transactions", base_identity, "wallet_transaction", occurred_at, item, payload_hash)

    def _persist_position(self, account: Account, expected_chain: str, item: dict[str, Any]) -> None:
        payload_hash = self._payload_hash(item)
        # The hash, not Zerion's abstract data.id, is the durable snapshot key.
        external_id = f"{expected_chain}:position:{payload_hash[:40]}"
        self._raw(
            account.id,
            "zerion:positions",
            external_id,
            "wallet_simple_position",
            self._aware(self._now()),
            item,
            payload_hash,
        )

    def _raw(
        self,
        account_id: UUID,
        source: str,
        external_id: str,
        event_kind: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> None:
        external_id = external_id[:256]
        existing = self.session.scalar(
            select(RawEvent).where(
                RawEvent.account_id == account_id,
                RawEvent.source == source,
                RawEvent.external_event_id == external_id,
            )
        )
        if existing and existing.payload_hash == payload_hash:
            self.stats.raw_existing += 1
            return
        if existing:
            external_id = f"{external_id[:230]}:rev:{payload_hash[:16]}"
            revision = self.session.scalar(
                select(RawEvent).where(
                    RawEvent.account_id == account_id,
                    RawEvent.source == source,
                    RawEvent.external_event_id == external_id,
                )
            )
            if revision:
                self.stats.raw_existing += 1
                return
            event_kind = f"{event_kind}_revision"
        self.session.add(
            RawEvent(
                account_id=account_id,
                source=source,
                external_event_id=external_id,
                event_kind=event_kind,
                occurred_at=occurred_at,
                payload_json=payload,
                payload_hash=payload_hash,
                status=RawEventStatus.RECEIVED,
            )
        )
        self.stats.raw_created += 1

    def _finish(
        self,
        run_id: UUID,
        source_id: UUID,
        status: SyncRunStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ProviderSyncRun:
        run = self.session.get(ProviderSyncRun, run_id)
        source = self.session.get(AccountDataSource, source_id)
        finished_at = self._aware(self._now())
        if not run or not source:
            raise RuntimeError("Zerion sync state disappeared")
        run.status = status
        run.stats_json = self.stats.as_dict()
        run.warnings_json = self.warnings
        run.error_code = error_code
        run.error_message = (error_message or "")[:300] or None
        run.finished_at = finished_at
        if run.request_count > 0:
            source.last_synced_at = finished_at
            source.next_sync_after = finished_at + timedelta(seconds=source.min_sync_interval_seconds)
        self.session.commit()
        self.session.refresh(run)
        return run

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return ZerionShadowSyncService._aware(parsed)
            except ValueError:
                pass
        return utc_now()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
