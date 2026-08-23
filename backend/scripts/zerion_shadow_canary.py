"""One-shot real Zerion shadow canary using an in-memory database.

The script never prints credentials or raw provider payloads. It uses the
public example address from Zerion's API documentation and exits non-zero if
the connector writes Ledger data, exceeds three requests, or fails upstream.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.connectors.zerion.sync import ZerionShadowSyncService
from app.core.config import Settings
from app.db import Base
from app.models import (
    Account,
    AccountDataSource,
    AccountKind,
    DataSourceMode,
    LedgerEvent,
    Portfolio,
    RawEvent,
    SyncRunStatus,
)

PUBLIC_CANARY_ADDRESS = "0x42b9df65b219b3dd36ff330a4dd8f327a6ada990"


def main() -> int:
    settings = Settings(_env_file=None)
    if not settings.zerion_enabled or not settings.zerion_api_key:
        print(json.dumps({"ok": False, "error": "ZERION_NOT_CONFIGURED"}))
        return 2

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        portfolio = Portfolio(name="Zerion public canary")
        session.add(portfolio)
        session.flush()
        account = Account(
            portfolio_id=portfolio.id,
            kind=AccountKind.WALLET,
            provider="evm",
            label="Zerion documentation example",
            external_account_id=f"1:{PUBLIC_CANARY_ADDRESS}",
            chain_id="1",
            address=PUBLIC_CANARY_ADDRESS,
        )
        session.add(account)
        session.flush()
        source = AccountDataSource(
            account_id=account.id,
            provider="zerion",
            mode=DataSourceMode.SHADOW,
            is_enabled=True,
            requests_per_second_limit=1,
            daily_request_limit=300,
            daily_request_budget=270,
            max_requests_per_run=3,
            min_sync_interval_seconds=900,
        )
        session.add(source)
        session.commit()

        ledger_before = session.scalar(select(func.count()).select_from(LedgerEvent)) or 0
        run = ZerionShadowSyncService(session, settings).run(account.id)
        ledger_after = session.scalar(select(func.count()).select_from(LedgerEvent)) or 0
        raw_count = session.scalar(select(func.count()).select_from(RawEvent)) or 0
        safe_rate_limits = {
            key: run.rate_limit_json.get(key)
            for key in ("tier", "second_limit", "second_remaining", "day_limit", "day_remaining", "day_reset_seconds")
            if key in run.rate_limit_json
        }
        ok = (
            run.status in {SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL}
            and run.request_count <= 3
            and ledger_before == ledger_after == 0
            and raw_count > 0
        )
        print(
            json.dumps(
                {
                    "ok": ok,
                    "status": run.status.value,
                    "request_count": run.request_count,
                    "raw_event_count": raw_count,
                    "ledger_before": ledger_before,
                    "ledger_after": ledger_after,
                    "stats": run.stats_json,
                    "warnings": run.warnings_json,
                    "error_code": run.error_code,
                    "rate_limits": safe_rate_limits,
                },
                ensure_ascii=False,
            )
        )
        return 0 if ok else 1
    finally:
        session.close()
        Base.metadata.drop_all(engine)


if __name__ == "__main__":
    raise SystemExit(main())
