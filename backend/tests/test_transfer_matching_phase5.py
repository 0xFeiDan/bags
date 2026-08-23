from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import Settings
from app.models import (
    Account,
    AccountKind,
    Asset,
    AssetType,
    EntryDirection,
    EventSource,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    Portfolio,
    RawEvent,
    RawEventStatus,
    SyncRunStatus,
    TransferCandidate,
    TransferCandidateStatus,
    TransferGroup,
    TransferGroupStatus,
)
from app.schemas import TransferMatchRequest
from app.services.transfer_matching import TransferMatchingService


def seed_foundation(db_session):
    portfolio = Portfolio(name="Transfer Portfolio")
    db_session.add(portfolio)
    db_session.flush()
    exchange = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.EXCHANGE,
        provider="binance",
        label="Binance",
        external_account_id="spot",
    )
    wallet = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.WALLET,
        provider="evm",
        label="Wallet",
        external_account_id="1:0x1111111111111111111111111111111111111111",
        address="0x1111111111111111111111111111111111111111",
        chain_id="1",
    )
    second_wallet = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.WALLET,
        provider="evm",
        label="Second Wallet",
        external_account_id="1:0x2222222222222222222222222222222222222222",
        address="0x2222222222222222222222222222222222222222",
        chain_id="1",
    )
    btc = Asset(canonical_symbol="BTC", name="Bitcoin", asset_type=AssetType.NATIVE, decimals=8)
    wbtc = Asset(
        canonical_symbol="WBTC",
        name="Wrapped Bitcoin",
        asset_type=AssetType.TOKEN,
        decimals=8,
        chain_id="1",
        contract_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    db_session.add_all([exchange, wallet, second_wallet, btc, wbtc])
    db_session.commit()
    return portfolio, exchange, wallet, second_wallet, btc, wbtc


def add_transfer_event(
    db_session,
    *,
    portfolio,
    account,
    asset,
    event_type,
    direction,
    amount,
    occurred_at,
    tx_hash=None,
    raw_id="event",
    fee=Decimal("0"),
):
    raw = RawEvent(
        account_id=account.id,
        source=account.provider,
        external_event_id=raw_id,
        event_kind="withdraw" if direction == EntryDirection.DEBIT else "deposit",
        occurred_at=occurred_at,
        payload_json={"id": raw_id, "txId": tx_hash},
        payload_hash=(raw_id.encode().hex() + "0" * 64)[:64],
        status=RawEventStatus.NORMALIZED,
    )
    db_session.add(raw)
    db_session.flush()
    event = LedgerEvent(
        portfolio_id=portfolio.id,
        raw_event_id=raw.id,
        event_type=event_type,
        source=EventSource.RAW,
        status=EventStatus.POSTED,
        occurred_at=occurred_at,
        tx_hash=tx_hash,
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        LedgerEntry(
            ledger_event_id=event.id,
            account_id=account.id,
            asset_id=asset.id,
            direction=direction,
            quantity=amount,
        )
    )
    if fee > 0:
        db_session.add(
            LedgerEntry(
                ledger_event_id=event.id,
                account_id=account.id,
                asset_id=asset.id,
                direction=EntryDirection.DEBIT,
                quantity=fee,
                fee_flag=True,
            )
        )
    db_session.commit()
    return event


def test_exact_tx_hash_auto_matches_and_preserves_fee_and_raw_events(db_session):
    portfolio, exchange, wallet, _, btc, _ = seed_foundation(db_session)
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    source = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=exchange,
        asset=btc,
        event_type=LedgerEventType.WITHDRAW,
        direction=EntryDirection.DEBIT,
        amount=Decimal("1"),
        fee=Decimal("0.0002"),
        occurred_at=now,
        tx_hash="0xabc",
        raw_id="withdraw-1",
    )
    destination = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=wallet,
        asset=btc,
        event_type=LedgerEventType.TRANSFER_IN,
        direction=EntryDirection.CREDIT,
        amount=Decimal("0.9998"),
        occurred_at=now + timedelta(minutes=5),
        tx_hash="0xABC",
        raw_id="deposit-1",
    )

    service = TransferMatchingService(db_session, Settings())
    run = service.run(portfolio.id, TransferMatchRequest())
    assert run.status == SyncRunStatus.SUCCEEDED, (run.error_code, run.error_message, run.warnings_json)
    assert run.stats_json["automatically_matched"] == 1
    candidate = db_session.scalar(select(TransferCandidate))
    group = db_session.scalar(select(TransferGroup))
    assert candidate.score == 100
    assert candidate.status == TransferCandidateStatus.AUTO_MATCHED
    assert candidate.score_breakdown_json["tx_hash_points"] == 60
    assert group.status == TransferGroupStatus.AUTO_MATCHED
    assert group.source_event_id == source.id
    assert group.destination_event_id == destination.id
    assert group.source_amount == Decimal("1")
    assert abs(group.destination_amount - Decimal("0.9998")) < Decimal("0.000000000000001")
    assert abs(group.fee_amount - Decimal("0.0002")) < Decimal("0.000000000000001")
    assert group.metadata_json["internal_portfolio_transfer"] is True
    assert db_session.get(LedgerEvent, source.id).event_type == LedgerEventType.WITHDRAW
    assert db_session.get(LedgerEvent, destination.id).event_type == LedgerEventType.TRANSFER_IN
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 2

    second = TransferMatchingService(db_session, Settings()).run(portfolio.id, TransferMatchRequest())
    assert second.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(TransferCandidate)) == 1
    assert db_session.scalar(select(func.count()).select_from(TransferGroup)) == 1
    assert db_session.scalar(select(TransferCandidate)).status == TransferCandidateStatus.AUTO_MATCHED


def test_without_hash_candidate_stays_unmatched_and_unknown_deposit_is_not_forced(db_session):
    portfolio, exchange, wallet, second_wallet, btc, _ = seed_foundation(db_session)
    now = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=exchange,
        asset=btc,
        event_type=LedgerEventType.TRANSFER_OUT,
        direction=EntryDirection.DEBIT,
        amount=Decimal("0.5"),
        occurred_at=now,
        raw_id="out-no-hash",
    )
    add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=wallet,
        asset=btc,
        event_type=LedgerEventType.TRANSFER_IN,
        direction=EntryDirection.CREDIT,
        amount=Decimal("0.5"),
        occurred_at=now + timedelta(minutes=10),
        raw_id="in-no-hash",
    )
    add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=second_wallet,
        asset=btc,
        event_type=LedgerEventType.TRANSFER_IN,
        direction=EntryDirection.CREDIT,
        amount=Decimal("3"),
        occurred_at=now + timedelta(days=10),
        raw_id="external-deposit",
    )
    run = TransferMatchingService(db_session, Settings()).run(portfolio.id, TransferMatchRequest())
    assert run.status == SyncRunStatus.SUCCEEDED
    candidates = list(db_session.scalars(select(TransferCandidate)))
    assert len(candidates) == 1
    assert candidates[0].score == 40
    assert candidates[0].status == TransferCandidateStatus.UNMATCHED
    assert db_session.scalar(select(func.count()).select_from(TransferGroup)) == 0


def test_review_candidate_can_be_confirmed_then_unmatched_without_deleting_evidence(db_session):
    portfolio, exchange, wallet, _, btc, _ = seed_foundation(db_session)
    now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
    source = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=exchange,
        asset=btc,
        event_type=LedgerEventType.WITHDRAW,
        direction=EntryDirection.DEBIT,
        amount=Decimal("1"),
        occurred_at=now,
        tx_hash="0xreview",
        raw_id="review-out",
    )
    destination = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=wallet,
        asset=btc,
        event_type=LedgerEventType.TRANSFER_IN,
        direction=EntryDirection.CREDIT,
        amount=Decimal("0.8"),
        occurred_at=now + timedelta(hours=2),
        tx_hash="0xreview",
        raw_id="review-in",
    )
    service = TransferMatchingService(db_session, Settings())
    run = service.run(portfolio.id, TransferMatchRequest())
    assert run.status == SyncRunStatus.SUCCEEDED
    candidate = db_session.scalar(select(TransferCandidate))
    assert candidate.score == 80
    assert candidate.status == TransferCandidateStatus.NEEDS_REVIEW
    group = service.confirm_candidate(candidate.id, "verified externally")
    assert group.status == TransferGroupStatus.CONFIRMED
    assert group.match_method == "manual_confirmation"
    unmatched = service.unmatch(group.id, "wrong destination")
    assert unmatched.status == TransferGroupStatus.UNMATCHED
    assert db_session.get(TransferCandidate, candidate.id).status == TransferCandidateStatus.REJECTED
    assert db_session.get(LedgerEvent, source.id) is not None
    assert db_session.get(LedgerEvent, destination.id) is not None
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 2


def test_manual_match_can_link_wrapped_asset_without_claiming_automatic_confidence(db_session):
    portfolio, exchange, wallet, _, btc, wbtc = seed_foundation(db_session)
    now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    source = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=exchange,
        asset=btc,
        event_type=LedgerEventType.WITHDRAW,
        direction=EntryDirection.DEBIT,
        amount=Decimal("1"),
        occurred_at=now,
        raw_id="btc-out",
    )
    destination = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=wallet,
        asset=wbtc,
        event_type=LedgerEventType.TRANSFER_IN,
        direction=EntryDirection.CREDIT,
        amount=Decimal("1"),
        occurred_at=now + timedelta(minutes=5),
        raw_id="wbtc-in",
    )
    group = TransferMatchingService(db_session, Settings()).manual_match(source.id, destination.id, "BTC wrapped to WBTC")
    assert group.status == TransferGroupStatus.CONFIRMED
    assert group.match_method == "manual"
    assert group.confidence_score == 20
    assert group.source_asset_id == btc.id
    assert group.destination_asset_id == wbtc.id


def test_transfer_group_keeps_fee_asset_separate_from_transferred_asset(db_session):
    portfolio, exchange, wallet, _, btc, wbtc = seed_foundation(db_session)
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    source = add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=exchange,
        asset=wbtc,
        event_type=LedgerEventType.TRANSFER_OUT,
        direction=EntryDirection.DEBIT,
        amount=Decimal("2"),
        occurred_at=now,
        tx_hash="0xcrossfee",
        raw_id="cross-fee-out",
    )
    db_session.add(
        LedgerEntry(
            ledger_event_id=source.id,
            account_id=exchange.id,
            asset_id=btc.id,
            direction=EntryDirection.DEBIT,
            quantity=Decimal("0.001"),
            fee_flag=True,
        )
    )
    add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=wallet,
        asset=wbtc,
        event_type=LedgerEventType.TRANSFER_IN,
        direction=EntryDirection.CREDIT,
        amount=Decimal("2"),
        occurred_at=now + timedelta(minutes=2),
        tx_hash="0xcrossfee",
        raw_id="cross-fee-in",
    )
    db_session.commit()
    run = TransferMatchingService(db_session, Settings()).run(portfolio.id, TransferMatchRequest())
    assert run.status == SyncRunStatus.SUCCEEDED
    group = db_session.scalar(select(TransferGroup))
    assert group.source_asset_id == wbtc.id
    assert group.fee_asset_id == btc.id
    assert abs(group.fee_amount - Decimal("0.001")) < Decimal("0.000000000000001")


def test_competing_exact_candidates_cannot_reuse_the_same_source_event(db_session):
    portfolio, exchange, wallet, second_wallet, btc, _ = seed_foundation(db_session)
    now = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)
    add_transfer_event(
        db_session,
        portfolio=portfolio,
        account=exchange,
        asset=btc,
        event_type=LedgerEventType.WITHDRAW,
        direction=EntryDirection.DEBIT,
        amount=Decimal("1"),
        occurred_at=now,
        tx_hash="0xduplicate",
        raw_id="duplicate-out",
    )
    for index, destination_account in enumerate((wallet, second_wallet), start=1):
        add_transfer_event(
            db_session,
            portfolio=portfolio,
            account=destination_account,
            asset=btc,
            event_type=LedgerEventType.TRANSFER_IN,
            direction=EntryDirection.CREDIT,
            amount=Decimal("1"),
            occurred_at=now + timedelta(minutes=index),
            tx_hash="0xduplicate",
            raw_id=f"duplicate-in-{index}",
        )
    run = TransferMatchingService(db_session, Settings()).run(portfolio.id, TransferMatchRequest())
    assert run.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(select(func.count()).select_from(TransferGroup)) == 1
    candidates = list(db_session.scalars(select(TransferCandidate).order_by(TransferCandidate.status)))
    assert {item.status for item in candidates} == {
        TransferCandidateStatus.AUTO_MATCHED,
        TransferCandidateStatus.NEEDS_REVIEW,
    }


def test_transfer_api_requires_existing_portfolio(client):
    response = client.post(
        "/api/v1/transfers/portfolios/00000000-0000-0000-0000-000000000000/match",
        json={},
    )
    assert response.status_code == 422


def test_manual_transfer_changes_require_recent_sensitive_auth(raw_client):
    registered = raw_client.post(
        "/api/v1/auth/register",
        json={"email": "transfer-security@example.com", "password": "Transfer-security-password-2026"},
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
    )
    assert registered.status_code == 201
    raw_client.headers["X-CSRF-Token"] = raw_client.cookies.get("bags_csrf")
    denied = raw_client.post(
        "/api/v1/transfers/manual",
        json={
            "source_event_id": "00000000-0000-0000-0000-000000000001",
            "destination_event_id": "00000000-0000-0000-0000-000000000002",
        },
    )
    assert denied.status_code == 403
