import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AccountDataSource, ProviderQuotaUsage, ProviderSyncRun

ZERION_PROVIDER = "zerion"
_PROCESS_QUOTA_LOCK = threading.Lock()
_POSTGRES_ADVISORY_LOCK_ID = 7_216_900_002


class ZerionBudgetExceeded(RuntimeError):
    pass


class ZerionRunBudgetExceeded(ZerionBudgetExceeded):
    pass


def extract_rate_limits(headers: Mapping[str, str]) -> dict[str, Any]:
    names = {
        "second_limit": "RateLimit-Org-Second-Limit",
        "second_remaining": "RateLimit-Org-Second-Remaining",
        "second_reset_seconds": "RateLimit-Org-Second-Reset",
        "day_limit": "RateLimit-Org-Day-Limit",
        "day_remaining": "RateLimit-Org-Day-Remaining",
        "day_reset_seconds": "RateLimit-Org-Day-Reset",
        "month_limit": "RateLimit-Org-Month-Limit",
        "month_remaining": "RateLimit-Org-Month-Remaining",
        "month_reset_seconds": "RateLimit-Org-Month-Reset",
        "tier": "RateLimit-Org-Tier",
    }
    result: dict[str, Any] = {}
    for key, header in names.items():
        value = headers.get(header)
        if value is None:
            continue
        if key == "tier":
            result[key] = value[:64]
            continue
        try:
            result[key] = int(value)
        except ValueError:
            continue
    return result


class ZerionRequestGovernor:
    """Persistently reserves request slots before network I/O.

    PostgreSQL advisory locking coordinates multiple workers. The in-process
    lock provides equivalent serialization for SQLite tests and the default
    single-worker deployment.
    """

    def __init__(
        self,
        session: Session,
        source: AccountDataSource,
        run: ProviderSyncRun,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.source = source
        self.run = run
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep

    @property
    def remaining_run_budget(self) -> int:
        return max(0, self.run.request_budget - self.run.request_count)

    def reserve(self) -> None:
        if self.run.request_count >= self.run.request_budget:
            raise ZerionRunBudgetExceeded("Zerion per-run request budget is exhausted")

        now = self._aware(self._now())
        with _PROCESS_QUOTA_LOCK:
            if self.session.get_bind().dialect.name == "postgresql":
                self.session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": _POSTGRES_ADVISORY_LOCK_ID},
                )
            usage = self.session.scalar(
                select(ProviderQuotaUsage)
                .where(
                    ProviderQuotaUsage.provider == ZERION_PROVIDER,
                    ProviderQuotaUsage.usage_date == now.date(),
                )
                .with_for_update()
            )
            if not usage:
                usage = ProviderQuotaUsage(
                    provider=ZERION_PROVIDER,
                    usage_date=now.date(),
                    request_limit=self.source.daily_request_limit,
                    request_budget=self.source.daily_request_budget,
                )
                self.session.add(usage)
                self.session.flush()

            server_remaining = self._integer(usage.rate_limit_json.get("day_remaining"))
            reserved = max(0, usage.request_limit - usage.request_budget)
            if server_remaining is not None and server_remaining <= reserved:
                self.session.rollback()
                raise ZerionBudgetExceeded("Zerion provider daily reserve has been reached")
            if usage.request_count >= min(usage.request_budget, self.source.daily_request_budget):
                self.session.rollback()
                raise ZerionBudgetExceeded("Zerion local daily request budget is exhausted")

            next_slot = self._aware(usage.next_request_at) if usage.next_request_at else now
            slot = max(now, next_slot)
            wait_seconds = max(0.0, (slot - now).total_seconds())
            server_second_limit = self._integer(usage.rate_limit_json.get("second_limit"))
            effective_second_limit = self.source.requests_per_second_limit
            if server_second_limit is not None and server_second_limit > 0:
                effective_second_limit = min(effective_second_limit, server_second_limit)
            # A 5% safety margin avoids crossing a provider window because of
            # timestamp rounding. The response header may only tighten this.
            interval = 1.05 / max(1, effective_second_limit)
            usage.next_request_at = slot + timedelta(seconds=interval)
            usage.request_count += 1
            self.run.request_count += 1
            self.session.commit()

        if wait_seconds > 0:
            self._sleep(wait_seconds)

    def record_rate_limits(self, values: dict[str, Any]) -> None:
        if not values:
            return
        now = self._aware(self._now())
        with _PROCESS_QUOTA_LOCK:
            usage = self.session.scalar(
                select(ProviderQuotaUsage).where(
                    ProviderQuotaUsage.provider == ZERION_PROVIDER,
                    ProviderQuotaUsage.usage_date == now.date(),
                )
            )
            if usage:
                usage.rate_limit_json = values
                server_second_limit = self._integer(values.get("second_limit"))
                if server_second_limit is not None and server_second_limit > 0:
                    safe_next_request = now + timedelta(seconds=1.05 / server_second_limit)
                    current_next_request = self._aware(usage.next_request_at) if usage.next_request_at else now
                    usage.next_request_at = max(current_next_request, safe_next_request)

                server_day_limit = self._integer(values.get("day_limit"))
                if server_day_limit is not None and server_day_limit > 0:
                    # Preserve at least 10% for manual diagnostics. A provider
                    # header is authoritative when it is below local config.
                    server_day_budget = max(1, server_day_limit * 9 // 10)
                    usage.request_limit = min(usage.request_limit, server_day_limit)
                    usage.request_budget = min(usage.request_budget, server_day_budget)
            self.run.rate_limit_json = values
            self.session.commit()

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
