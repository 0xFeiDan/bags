import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.connectors.zerion.limits import ZerionBudgetExceeded, ZerionRequestGovernor
from app.connectors.zerion.sync import ZerionShadowSyncService
from app.core.config import Settings
from app.models import (
    Account,
    AccountDataSource,
    AccountKind,
    DataSourceMode,
    LedgerEvent,
    Portfolio,
    ProviderQuotaUsage,
    ProviderSyncRun,
    RawEvent,
    SyncRunStatus,
)

NOW = datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)
ADDRESS = "0x1111111111111111111111111111111111111111"
RATE_HEADERS = {
    "RateLimit-Org-Second-Limit": "1",
    "RateLimit-Org-Second-Remaining": "0",
    "RateLimit-Org-Second-Reset": "1",
    "RateLimit-Org-Day-Limit": "300",
    "RateLimit-Org-Day-Remaining": "299",
    "RateLimit-Org-Day-Reset": "3600",
    "RateLimit-Org-Tier": "demo",
}


def seed_source(db_session, *, daily_budget: int = 270):
    portfolio = Portfolio(name="Zerion Phase 2")
    db_session.add(portfolio)
    db_session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.WALLET,
        provider="evm",
        label="Base wallet",
        external_account_id=f"8453:{ADDRESS}",
        chain_id="8453",
        address=ADDRESS,
    )
    db_session.add(account)
    db_session.flush()
    source = AccountDataSource(
        account_id=account.id,
        provider="zerion",
        mode=DataSourceMode.SHADOW,
        is_enabled=True,
        requests_per_second_limit=1,
        daily_request_limit=300,
        daily_request_budget=daily_budget,
        max_requests_per_run=3,
        min_sync_interval_seconds=900,
    )
    db_session.add(source)
    db_session.commit()
    return account, source


def settings():
    return Settings(
        _env_file=None,
        zerion_enabled=True,
        zerion_api_key="test-zerion-key",
        zerion_base_url="https://api.zerion.test",
    )


def transaction(tx_hash: str, mined_at: str):
    return {
        "type": "transactions",
        "id": f"abstract-{tx_hash[-4:]}",
        "attributes": {
            "operation_type": "transfer",
            "hash": tx_hash,
            "mined_at": mined_at,
            "transfers": [],
        },
        "relationships": {"chain": {"data": {"type": "chains", "id": "base"}}},
    }


def test_shadow_sync_uses_three_requests_persists_raw_only_and_is_idempotent(db_session):
    account, source = seed_source(db_session)
    seen: list[httpx.Request] = []
    tx_recent = transaction("0x" + "a" * 64, "2026-08-23T01:59:00+00:00")
    tx_backfill = transaction("0x" + "b" * 64, "2026-08-22T12:00:00+00:00")
    position = {
        "type": "positions",
        "id": "abstract-position-id",
        "attributes": {
            "name": "USDC",
            "position_type": "wallet",
            "quantity": {"numeric": "100.0"},
            "value": 100.0,
        },
        "relationships": {"chain": {"data": {"type": "chains", "id": "base"}}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("Authorization", "").startswith("Basic ")
        if request.url.path.endswith("/positions/"):
            assert request.url.params["filter[chain_ids]"] == "base"
            assert request.url.params["filter[positions]"] == "only_simple"
            return httpx.Response(200, headers=RATE_HEADERS, json={"links": {"next": None}, "data": [position]})
        if request.url.params.get("page[after]") == "older":
            return httpx.Response(200, headers=RATE_HEADERS, json={"links": {"next": None}, "data": [tx_backfill]})
        assert request.url.params["filter[chain_ids]"] == "base"
        next_url = f"https://api.zerion.test/v1/wallets/{ADDRESS}/transactions/?page%5Bafter%5D=older"
        return httpx.Response(200, headers=RATE_HEADERS, json={"links": {"next": next_url}, "data": [tx_recent]})

    service = ZerionShadowSyncService(
        db_session,
        settings(),
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    first = service.run(account.id)

    assert first.status == SyncRunStatus.SUCCEEDED
    assert first.request_count == 3
    assert first.stats_json == {
        "pages_collected": 3,
        "transactions_seen": 2,
        "positions_seen": 1,
        "raw_created": 3,
        "raw_existing": 0,
        "ledger_created": 0,
    }
    assert first.rate_limit_json["day_limit"] == 300
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 3
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 0
    assert len(seen) == 3
    serialized_raw = json.dumps([item.payload_json for item in db_session.scalars(select(RawEvent))])
    assert "test-zerion-key" not in serialized_raw

    source = db_session.get(AccountDataSource, source.id)
    source.next_sync_after = None
    db_session.commit()
    second = ZerionShadowSyncService(
        db_session,
        settings(),
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        sleep=lambda _seconds: None,
    ).run(account.id)

    assert second.status == SyncRunStatus.SUCCEEDED
    assert second.request_count == 2  # historical backfill was already complete
    assert second.stats_json["raw_created"] == 0
    assert second.stats_json["raw_existing"] == 2
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 3
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 0
    usage = db_session.scalar(select(ProviderQuotaUsage))
    assert usage is not None and usage.request_count == 5
    assert usage.request_budget == 270


def test_request_governor_spaces_calls_and_stops_at_daily_budget(db_session):
    _account, source = seed_source(db_session, daily_budget=2)
    run = ProviderSyncRun(data_source_id=source.id, request_kind="test", request_budget=3)
    db_session.add(run)
    db_session.commit()
    sleeps: list[float] = []
    governor = ZerionRequestGovernor(
        db_session,
        source,
        run,
        now=lambda: NOW,
        sleep=sleeps.append,
    )

    governor.reserve()
    governor.reserve()
    with pytest.raises(ZerionBudgetExceeded, match="daily request budget"):
        governor.reserve()

    assert run.request_count == 2
    assert sleeps == pytest.approx([1.05])


def test_request_governor_tightens_legacy_caps_from_provider_headers(db_session):
    _account, source = seed_source(db_session, daily_budget=1800)
    source.requests_per_second_limit = 3
    source.daily_request_limit = 2000
    run = ProviderSyncRun(data_source_id=source.id, request_kind="test", request_budget=3)
    db_session.add(run)
    db_session.commit()
    sleeps: list[float] = []
    governor = ZerionRequestGovernor(
        db_session,
        source,
        run,
        now=lambda: NOW,
        sleep=sleeps.append,
    )

    governor.reserve()
    governor.record_rate_limits({"second_limit": 1, "day_limit": 300, "day_remaining": 299})
    governor.reserve()

    usage = db_session.scalar(select(ProviderQuotaUsage))
    assert usage is not None
    assert usage.request_limit == 300
    assert usage.request_budget == 270
    assert sleeps == pytest.approx([1.05])


def test_shadow_sync_does_not_retry_429(db_session):
    account, _source = seed_source(db_session)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={**RATE_HEADERS, "RateLimit-Org-Second-Remaining": "0"},
            json={"errors": [{"title": "Too many requests", "detail": "throttled"}]},
        )

    run = ZerionShadowSyncService(
        db_session,
        settings(),
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        sleep=lambda _seconds: None,
    ).run(account.id)

    assert run.status == SyncRunStatus.FAILED
    assert run.error_code == "ZERION_RATE_LIMITED"
    assert run.request_count == 1
    assert calls == 1
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 0
