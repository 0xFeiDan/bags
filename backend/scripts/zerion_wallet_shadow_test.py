"""Run a credential-safe, persistent Zerion shadow test for one EVM wallet."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.connectors.evm.chains import resolve_chain
from app.connectors.evm.collector import normalize_address
from app.connectors.zerion.sync import ZERION_CHAIN_IDS, ZerionShadowSyncService, ZerionSyncRejected
from app.core.config import Settings
from app.models import (
    Account,
    AccountDataSource,
    AccountKind,
    DataSourceMode,
    LedgerEvent,
    Portfolio,
    ProviderQuotaUsage,
    RawEvent,
    SyncRunStatus,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--chain", default="arbitrum")
    parser.add_argument("--portfolio", default="Zerion Shadow Test")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings(_env_file=None)
    if not settings.zerion_enabled or not settings.zerion_api_key:
        print(json.dumps({"ok": False, "error_code": "ZERION_NOT_CONFIGURED"}))
        return 2

    chain = resolve_chain(args.chain)
    address = normalize_address(args.address)
    if not chain or chain.key not in ZERION_CHAIN_IDS:
        print(json.dumps({"ok": False, "error_code": "UNSUPPORTED_CHAIN"}))
        return 2
    if not address.startswith("0x") or len(address) != 42:
        print(json.dumps({"ok": False, "error_code": "INVALID_EVM_ADDRESS"}))
        return 2

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as session:
        portfolio = session.scalar(select(Portfolio).where(Portfolio.name == args.portfolio))
        portfolio_created = portfolio is None
        if portfolio is None:
            portfolio = Portfolio(name=args.portfolio, base_currency="USD")
            session.add(portfolio)
            session.flush()

        account = session.scalar(
            select(Account).where(
                Account.provider == "evm",
                Account.chain_id == chain.chain_id,
                func.lower(Account.address) == address,
            )
        )
        account_created = account is None
        if account is None:
            account = Account(
                portfolio_id=portfolio.id,
                kind=AccountKind.WALLET,
                provider="evm",
                label=f"{chain.name} wallet",
                external_account_id=f"{chain.chain_id}:{address}",
                chain_id=chain.chain_id,
                address=address,
            )
            session.add(account)
            session.flush()
        else:
            if portfolio_created and account.portfolio_id != portfolio.id:
                session.delete(portfolio)
                session.flush()
                portfolio_created = False
            portfolio = session.get(Portfolio, account.portfolio_id)
            if portfolio is None:
                raise RuntimeError("wallet portfolio is missing")

        source = session.scalar(
            select(AccountDataSource).where(
                AccountDataSource.account_id == account.id,
                AccountDataSource.provider == "zerion",
            )
        )
        source_created = source is None
        if source is None:
            source = AccountDataSource(account_id=account.id, provider="zerion")
            session.add(source)
        source.mode = DataSourceMode.SHADOW
        source.is_enabled = True
        source.requests_per_second_limit = max(1, min(settings.zerion_requests_per_second_limit, 1))
        source.daily_request_limit = max(1, min(settings.zerion_daily_request_limit, 300))
        source.daily_request_budget = max(1, min(settings.zerion_daily_request_budget, 270))
        source.max_requests_per_run = max(1, min(settings.zerion_max_requests_per_run, 3))
        source.min_sync_interval_seconds = max(settings.zerion_min_sync_interval_seconds, 900)
        session.commit()

        ledger_before = session.scalar(
            select(func.count()).select_from(LedgerEvent).where(LedgerEvent.portfolio_id == portfolio.id)
        ) or 0
        raw_before = session.scalar(
            select(func.count()).select_from(RawEvent).where(RawEvent.account_id == account.id)
        ) or 0

        try:
            run = ZerionShadowSyncService(session, settings).run(account.id)
        except ZerionSyncRejected as error:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": "ZERION_SYNC_REJECTED",
                        "retry_after_seconds": error.retry_after_seconds,
                    }
                )
            )
            return 2

        ledger_after = session.scalar(
            select(func.count()).select_from(LedgerEvent).where(LedgerEvent.portfolio_id == portfolio.id)
        ) or 0
        raw_after = session.scalar(
            select(func.count()).select_from(RawEvent).where(RawEvent.account_id == account.id)
        ) or 0
        usage = session.scalar(
            select(ProviderQuotaUsage)
            .where(ProviderQuotaUsage.provider == "zerion")
            .order_by(ProviderQuotaUsage.usage_date.desc())
        )
        safe_rate_limits = {
            key: run.rate_limit_json.get(key)
            for key in (
                "tier",
                "second_limit",
                "second_remaining",
                "day_limit",
                "day_remaining",
                "day_reset_seconds",
            )
            if key in run.rate_limit_json
        }
        ok = (
            run.status in {SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL}
            and run.request_count <= 3
            and ledger_before == ledger_after
            and run.stats_json.get("ledger_created") == 0
        )
        print(
            json.dumps(
                {
                    "ok": ok,
                    "chain": chain.key,
                    "portfolio_created": portfolio_created,
                    "account_created": account_created,
                    "source_created": source_created,
                    "account_id": str(account.id),
                    "status": run.status.value,
                    "request_count": run.request_count,
                    "raw_before": raw_before,
                    "raw_after": raw_after,
                    "raw_created_this_run": raw_after - raw_before,
                    "ledger_before": ledger_before,
                    "ledger_after": ledger_after,
                    "stats": run.stats_json,
                    "warnings": run.warnings_json,
                    "error_code": run.error_code,
                    "rate_limits": safe_rate_limits,
                    "local_daily_request_count": usage.request_count if usage else 0,
                    "local_daily_request_budget": usage.request_budget if usage else 0,
                },
                ensure_ascii=False,
            )
        )
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
