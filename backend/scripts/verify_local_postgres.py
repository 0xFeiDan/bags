"""Print a credential-free readiness summary for the configured Bags database."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from app.core.config import Settings


ZERION_TABLES = (
    "account_data_sources",
    "provider_sync_runs",
    "provider_sync_cursors",
    "provider_quota_usage",
)


def main() -> None:
    settings = Settings(_env_file=None)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        summary = {
            "db_version": connection.execute(text("SHOW server_version")).scalar_one(),
            "migration": connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
            "public_table_count": connection.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            ).scalar_one(),
            "zerion_tables": {
                name: bool(
                    connection.execute(
                        text("SELECT to_regclass(:table_name)"),
                        {"table_name": f"public.{name}"},
                    ).scalar_one_or_none()
                )
                for name in ZERION_TABLES
            },
            "zerion_source_rows": connection.execute(
                text("SELECT count(*) FROM account_data_sources WHERE provider = 'zerion'")
            ).scalar_one(),
            "portfolio_rows": connection.execute(text("SELECT count(*) FROM portfolios")).scalar_one(),
            "account_rows": connection.execute(text("SELECT count(*) FROM accounts")).scalar_one(),
            "zerion_raw_event_rows": connection.execute(
                text("SELECT count(*) FROM raw_events WHERE source LIKE 'zerion:%'")
            ).scalar_one(),
            "ledger_event_rows": connection.execute(text("SELECT count(*) FROM ledger_events")).scalar_one(),
            "zerion_sync_run_rows": connection.execute(
                text(
                    "SELECT count(*) FROM provider_sync_runs r "
                    "JOIN account_data_sources s ON s.id = r.data_source_id "
                    "WHERE s.provider = 'zerion'"
                )
            ).scalar_one(),
            "limits": {
                "requests_per_second": settings.zerion_requests_per_second_limit,
                "daily_limit": settings.zerion_daily_request_limit,
                "daily_budget": settings.zerion_daily_request_budget,
                "max_requests_per_run": settings.zerion_max_requests_per_run,
            },
            "zerion_enabled": settings.zerion_enabled,
            "zerion_key_configured": bool(settings.zerion_api_key),
        }
    engine.dispose()
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
