from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select

from app.models import (
    Account,
    AccountEquitySnapshot,
    AccountKind,
    Asset,
    AssetPrice,
    AssetType,
    EntryDirection,
    EventSource,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    Portfolio,
    PositionSnapshot,
    RawEvent,
    RawEventStatus,
    TransferGroup,
    TransferGroupStatus,
)
from app.services.dashboard import DashboardService


def seed_foundation(db_session, name="Dashboard Portfolio"):
    portfolio = Portfolio(name=name)
    db_session.add(portfolio)
    db_session.flush()
    exchange = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.EXCHANGE,
        provider="binance",
        label="Binance Spot",
        external_account_id="spot",
    )
    wallet = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.WALLET,
        provider="evm",
        label="Ethereum Wallet",
        external_account_id="1:0x1111111111111111111111111111111111111111",
        chain_id="1",
        address="0x1111111111111111111111111111111111111111",
    )
    perp = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.PERP_DEX,
        provider="hyperliquid",
        label="Hyperliquid",
        external_account_id="0x2222222222222222222222222222222222222222",
    )
    btc = Asset(canonical_symbol="BTC", name="Bitcoin", asset_type=AssetType.NATIVE, decimals=8)
    usdt = Asset(canonical_symbol="USDT", name="Tether", asset_type=AssetType.STABLECOIN, decimals=6)
    token = Asset(canonical_symbol="UNKNOWN", name="Unknown Token", asset_type=AssetType.TOKEN, decimals=18)
    db_session.add_all([exchange, wallet, perp, btc, usdt, token])
    db_session.flush()
    db_session.commit()
    return portfolio, exchange, wallet, perp, btc, usdt, token


def add_event(db_session, portfolio, event_type, occurred_at, legs):
    event = LedgerEvent(
        portfolio_id=portfolio.id,
        event_type=event_type,
        source=EventSource.MANUAL,
        status=EventStatus.POSTED,
        occurred_at=occurred_at,
    )
    db_session.add(event)
    db_session.flush()
    for account, asset, direction, quantity, fee in legs:
        db_session.add(
            LedgerEntry(
                ledger_event_id=event.id,
                account_id=account.id,
                asset_id=asset.id,
                direction=direction,
                quantity=Decimal(quantity),
                fee_flag=fee,
            )
        )
        if asset.asset_type == AssetType.STABLECOIN:
            db_session.add(AssetPrice(asset_id=asset.id, price_usd=Decimal("1"), source="test", as_of=occurred_at))
    db_session.commit()
    return event


def add_perp_state(db_session, account, at, *, equity, unrealized, margin, quantity, mark):
    raw = RawEvent(
        account_id=account.id,
        source="hyperliquid",
        external_event_id=f"state:{at.isoformat()}",
        event_kind="clearinghouse_state",
        occurred_at=at,
        payload_json={"at": at.isoformat()},
        payload_hash=(at.isoformat().encode().hex() + "0" * 64)[:64],
        status=RawEventStatus.NORMALIZED,
    )
    db_session.add(raw)
    db_session.flush()
    db_session.add_all(
        [
            AccountEquitySnapshot(
                account_id=account.id,
                source_raw_event_id=raw.id,
                provider="hyperliquid",
                currency="USDT",
                equity=Decimal(equity),
                margin_used=Decimal(margin),
                unrealized_pnl=Decimal(unrealized),
                as_of=at,
            ),
            PositionSnapshot(
                account_id=account.id,
                source_raw_event_id=raw.id,
                product="hyperliquid_perp",
                symbol="BTC",
                position_side="LONG",
                quantity=Decimal(quantity),
                entry_price=Decimal("500"),
                mark_price=Decimal(mark),
                unrealized_pnl=Decimal(unrealized),
                leverage=Decimal("3"),
                liquidation_price=Decimal("300"),
                notional=abs(Decimal(quantity) * Decimal(mark)),
                margin_asset="USDC",
                isolated=False,
                as_of=at,
                metadata_json={"source_quantity_unit": "base_asset", "margin_used": margin},
            ),
        ]
    )
    db_session.commit()


def test_dashboard_snapshot_excludes_external_cash_flow_from_investment_pnl(db_session):
    portfolio, exchange, _, perp, btc, usdt, _ = seed_foundation(db_session)
    first_at = datetime.now(timezone.utc) - timedelta(days=3)
    second_at = first_at + timedelta(days=1)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, first_at - timedelta(hours=2), [(exchange, usdt, EntryDirection.CREDIT, "1000", False)])
    add_event(
        db_session,
        portfolio,
        LedgerEventType.BUY,
        first_at - timedelta(hours=1),
        [
            (exchange, btc, EntryDirection.CREDIT, "1", False),
            (exchange, usdt, EntryDirection.DEBIT, "500", False),
        ],
    )
    db_session.add_all(
        [
            AssetPrice(asset_id=btc.id, price_usd=Decimal("600"), source="test", as_of=first_at),
            AssetPrice(asset_id=btc.id, price_usd=Decimal("650"), source="test", as_of=second_at),
        ]
    )
    db_session.commit()
    add_perp_state(db_session, perp, first_at, equity="200", unrealized="10", margin="40", quantity="0.5", mark="600")

    first = DashboardService(db_session).capture_snapshot(portfolio.id, first_at)
    assert first.total_nav == Decimal("1300")
    assert first.external_flow is None
    assert first.investment_pnl is None

    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, second_at - timedelta(minutes=30), [(exchange, usdt, EntryDirection.CREDIT, "100", False)])
    add_perp_state(db_session, perp, second_at, equity="220", unrealized="20", margin="50", quantity="0.5", mark="650")
    second = DashboardService(db_session).capture_snapshot(portfolio.id, second_at)
    assert second.total_nav == Decimal("1470")
    assert second.external_flow == Decimal("100")
    assert second.investment_pnl == Decimal("70")

    summary = DashboardService(db_session).summary(portfolio.id, run_id=second.source_cost_run_id)
    assert summary.total_net_worth_usd == Decimal("1470")
    assert summary.spot_value_usd == Decimal("650")
    assert summary.cash_usd == Decimal("600")
    assert summary.perp_equity_usd == Decimal("220")
    assert summary.unrealized_pnl_usd == Decimal("170")
    btc_exposure = next(row for row in summary.exposures if row.symbol == "BTC")
    assert btc_exposure.net_quantity == Decimal("1.5")
    assert btc_exposure.gross_long_usd == Decimal("975")
    assert summary.margin_usage_percent > Decimal("22")
    assert summary.health.valuation_complete is True
    hyperliquid = next(row for row in summary.accounts if row.account_id == perp.id)
    assert hyperliquid.unrealized_pnl_usd == Decimal("20")


def test_internal_transfer_does_not_change_nav_or_external_flow(db_session):
    portfolio, exchange, wallet, _, _, usdt, _ = seed_foundation(db_session)
    first_at = datetime.now(timezone.utc) - timedelta(days=3)
    second_at = first_at + timedelta(days=1)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, first_at - timedelta(hours=1), [(exchange, usdt, EntryDirection.CREDIT, "100", False)])
    first = DashboardService(db_session).capture_snapshot(portfolio.id, first_at)
    source = add_event(db_session, portfolio, LedgerEventType.TRANSFER_OUT, second_at - timedelta(hours=1), [(exchange, usdt, EntryDirection.DEBIT, "100", False)])
    destination = add_event(db_session, portfolio, LedgerEventType.TRANSFER_IN, second_at - timedelta(minutes=50), [(wallet, usdt, EntryDirection.CREDIT, "100", False)])
    db_session.add(
        TransferGroup(
            reference="TRF_DASH_0001",
            portfolio_id=portfolio.id,
            source_event_id=source.id,
            destination_event_id=destination.id,
            source_account_id=exchange.id,
            destination_account_id=wallet.id,
            source_asset_id=usdt.id,
            destination_asset_id=usdt.id,
            source_amount=Decimal("100"),
            destination_amount=Decimal("100"),
            fee_amount=Decimal("0"),
            source_occurred_at=source.occurred_at,
            destination_occurred_at=destination.occurred_at,
            status=TransferGroupStatus.CONFIRMED,
            confidence_score=100,
            match_method="test",
        )
    )
    db_session.commit()
    second = DashboardService(db_session).capture_snapshot(portfolio.id, second_at)
    assert first.total_nav == Decimal("100")
    assert second.total_nav == Decimal("100")
    assert second.external_flow == Decimal("0")
    assert second.investment_pnl == Decimal("0")


def test_usdt_balance_and_usdc_equity_are_fixed_at_one_dollar_without_prices(db_session):
    portfolio, exchange, _, perp, _, usdt, _ = seed_foundation(db_session)
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    add_event(
        db_session,
        portfolio,
        LedgerEventType.DEPOSIT,
        at - timedelta(minutes=10),
        [(exchange, usdt, EntryDirection.CREDIT, "100", False)],
    )
    add_perp_state(db_session, perp, at, equity="25", unrealized="2", margin="5", quantity="0.1", mark="500")
    equity = db_session.scalar(select(AccountEquitySnapshot).where(AccountEquitySnapshot.account_id == perp.id))
    equity.currency = "USDC"
    db_session.execute(delete(AssetPrice))
    db_session.commit()

    snapshot = DashboardService(db_session).capture_snapshot(portfolio.id, at)
    summary = DashboardService(db_session).summary(portfolio.id, run_id=snapshot.source_cost_run_id)

    assert snapshot.total_nav == Decimal("125")
    assert summary.cash_usd == Decimal("100")
    assert summary.perp_equity_usd == Decimal("25")
    assert summary.health.valuation_complete is True


def test_missing_price_returns_incomplete_nav_instead_of_false_zero(db_session):
    portfolio, _, wallet, _, _, _, token = seed_foundation(db_session)
    at = datetime.now(timezone.utc) - timedelta(days=1)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, at - timedelta(hours=1), [(wallet, token, EntryDirection.CREDIT, "2", False)])
    snapshot = DashboardService(db_session).capture_snapshot(portfolio.id, at)
    assert snapshot.total_nav is None
    assert snapshot.valuation_complete is False
    summary = DashboardService(db_session).summary(portfolio.id, run_id=snapshot.source_cost_run_id)
    assert summary.total_net_worth_usd is None
    assert summary.health.valuation_complete is False
    assert any("price" in warning.lower() or "unknown cost" in warning.lower() for warning in summary.health.warnings)


def test_stale_price_returns_incomplete_nav_instead_of_reusing_old_value(db_session):
    portfolio, _, wallet, _, _, _, token = seed_foundation(db_session)
    observed_at = datetime.now(timezone.utc) - timedelta(days=3)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, observed_at, [(wallet, token, EntryDirection.CREDIT, "2", False)])
    db_session.add(AssetPrice(asset_id=token.id, price_usd=Decimal("42"), source="test", as_of=observed_at))
    db_session.commit()

    snapshot = DashboardService(db_session).capture_snapshot(portfolio.id, datetime.now(timezone.utc))
    assert snapshot.total_nav is None
    assert snapshot.valuation_complete is False
    summary = DashboardService(db_session).summary(portfolio.id, run_id=snapshot.source_cost_run_id)
    assert summary.total_net_worth_usd is None
    assert any("missing a current USD price" in warning for warning in summary.health.warnings)


def test_dashboard_http_api_captures_and_reads_real_summary(client):
    portfolio = client.post("/api/v1/portfolios", json={"name": "HTTP Dashboard"}).json()
    account = client.post(
        "/api/v1/accounts",
        json={"portfolio_id": portfolio["id"], "kind": "wallet", "provider": "manual", "label": "Cash Vault", "external_account_id": "cash-vault"},
    ).json()
    asset = client.post(
        "/api/v1/assets",
            json={"canonical_symbol": "USD", "name": "US Dollar", "asset_type": "fiat", "decimals": 2},
    ).json()
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    ledger = client.post(
        "/api/v1/ledger/events",
        json={
            "portfolio_id": portfolio["id"],
            "event_type": "deposit",
            "source": "manual",
            "status": "posted",
            "occurred_at": occurred.isoformat(),
            "entries": [{"account_id": account["id"], "asset_id": asset["id"], "direction": "credit", "quantity": "250"}],
        },
    )
    assert ledger.status_code == 201, ledger.text
    captured = client.post(f"/api/v1/dashboard/portfolios/{portfolio['id']}/snapshots", json={})
    assert captured.status_code == 201, captured.text
    assert captured.json()["total_nav"] == "250.000000000000000000"
    summary = client.get(f"/api/v1/dashboard/portfolios/{portfolio['id']}/summary")
    assert summary.status_code == 200, summary.text
    payload = summary.json()
    assert payload["total_net_worth_usd"] == "250.000000000000000000"
    assert payload["cash_usd"] == "250.000000000000000000"
    assert payload["health"]["valuation_complete"] is True
