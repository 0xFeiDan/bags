from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select

from app.models import (
    Account,
    AccountKind,
    Asset,
    AssetPrice,
    AssetType,
    BalanceSnapshot,
    CostBasisOverride,
    CostLot,
    CostMethod,
    CostOverrideType,
    EntryDirection,
    EventSource,
    EventStatus,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    PositionCostSnapshot,
    RealizedPnlRecord,
    SyncRunStatus,
    TransferGroup,
    TransferGroupStatus,
)
from app.schemas import CostBasisRunRequest
from app.services.cost_basis import CostBasisService


def seed_foundation(db_session, name="Cost Portfolio"):
    from app.models import Portfolio

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
        label="Wallet",
        external_account_id="1:0x1111111111111111111111111111111111111111",
        chain_id="1",
        address="0x1111111111111111111111111111111111111111",
    )
    btc = Asset(canonical_symbol="BTC", name="Bitcoin", asset_type=AssetType.NATIVE, decimals=8)
    eth = Asset(canonical_symbol="ETH", name="Ether", asset_type=AssetType.NATIVE, decimals=18)
    usdt = Asset(canonical_symbol="USDT", name="Tether", asset_type=AssetType.STABLECOIN, decimals=6)
    usdc = Asset(canonical_symbol="USDC", name="USD Coin", asset_type=AssetType.STABLECOIN, decimals=6)
    db_session.add_all([exchange, wallet, btc, eth, usdt, usdc])
    db_session.flush()
    db_session.commit()
    return portfolio, exchange, wallet, btc, eth, usdt, usdc


def add_event(db_session, portfolio, event_type, occurred_at, legs, *, metadata=None):
    event = LedgerEvent(
        portfolio_id=portfolio.id,
        event_type=event_type,
        source=EventSource.MANUAL,
        status=EventStatus.POSTED,
        occurred_at=occurred_at,
        metadata_json=metadata or {},
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


def test_average_cost_carries_lots_through_transfer_without_realized_pnl(db_session):
    portfolio, exchange, wallet, btc, _, usdt, _ = seed_foundation(db_session)
    start = datetime.now(timezone.utc) - timedelta(days=10)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, start, [(exchange, usdt, EntryDirection.CREDIT, "80080", False)])
    add_event(
        db_session,
        portfolio,
        LedgerEventType.BUY,
        start + timedelta(hours=1),
        [
                (exchange, btc, EntryDirection.CREDIT, "1", False),
            (exchange, usdt, EntryDirection.DEBIT, "80000", False),
            (exchange, usdt, EntryDirection.DEBIT, "80", True),
        ],
    )
    source = add_event(
        db_session,
        portfolio,
        LedgerEventType.WITHDRAW,
        start + timedelta(days=1),
        [
            (exchange, btc, EntryDirection.DEBIT, "1", False),
            (exchange, btc, EntryDirection.DEBIT, "0.0002", True),
        ],
    )
    destination = add_event(
        db_session,
        portfolio,
        LedgerEventType.TRANSFER_IN,
        start + timedelta(days=1, minutes=5),
        [(wallet, btc, EntryDirection.CREDIT, "0.9998", False)],
    )
    group = TransferGroup(
        reference="TRF_COST_0001",
        portfolio_id=portfolio.id,
        source_event_id=source.id,
        destination_event_id=destination.id,
        source_account_id=exchange.id,
        destination_account_id=wallet.id,
        source_asset_id=btc.id,
        destination_asset_id=btc.id,
        source_amount=Decimal("1"),
        destination_amount=Decimal("0.9998"),
        fee_amount=Decimal("0.0002"),
        fee_asset_id=btc.id,
        source_occurred_at=source.occurred_at,
        destination_occurred_at=destination.occurred_at,
        status=TransferGroupStatus.CONFIRMED,
        confidence_score=100,
        match_method="test",
        metadata_json={"internal_portfolio_transfer": True},
    )
    db_session.add(group)
    db_session.commit()
    sale = add_event(
        db_session,
        portfolio,
        LedgerEventType.SELL,
        start + timedelta(days=2),
        [
            (wallet, btc, EntryDirection.DEBIT, "0.4", False),
            (wallet, usdt, EntryDirection.CREDIT, "40000", False),
            (wallet, usdt, EntryDirection.DEBIT, "40", True),
        ],
    )

    run = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest(method=CostMethod.AVERAGE_COST))
    assert run.status == SyncRunStatus.SUCCEEDED, (run.error_code, run.error_message, run.warnings_json)
    db_session.refresh(group)
    assert abs(group.original_cost_basis - Decimal("80063.984")) < Decimal("0.000000001")
    wallet_btc = db_session.scalar(
        select(PositionCostSnapshot).where(
            PositionCostSnapshot.run_id == run.id,
            PositionCostSnapshot.account_id == wallet.id,
            PositionCostSnapshot.asset_id == btc.id,
        )
    )
    assert wallet_btc.quantity == Decimal("0.599800000000000000")
    assert abs(wallet_btc.calculated_cost_usd - Decimal("48031.984")) < Decimal("0.000000001")
    sale_pnl = db_session.scalar(
        select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id, RealizedPnlRecord.ledger_event_id == sale.id, RealizedPnlRecord.category == "spot")
    )
    assert abs(sale_pnl.cost_basis_usd - Decimal("32032")) < Decimal("0.000000001")
    assert abs(sale_pnl.realized_pnl_usd - Decimal("7928")) < Decimal("0.000000001")
    transfer_spot_pnl = db_session.scalar(
        select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id, RealizedPnlRecord.ledger_event_id == source.id, RealizedPnlRecord.category == "spot")
    )
    assert transfer_spot_pnl is None


def test_same_asset_transfer_fee_is_consumed_when_it_is_an_extra_debit(db_session):
    portfolio, exchange, wallet, btc, _, usdt, _ = seed_foundation(db_session)
    start = datetime.now(timezone.utc) - timedelta(days=2)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, start, [(exchange, usdt, EntryDirection.CREDIT, "100020", False)])
    add_event(
        db_session,
        portfolio,
        LedgerEventType.BUY,
        start + timedelta(minutes=1),
        [(exchange, btc, EntryDirection.CREDIT, "1.0002", False), (exchange, usdt, EntryDirection.DEBIT, "100020", False)],
    )
    source = add_event(
        db_session,
        portfolio,
        LedgerEventType.WITHDRAW,
        start + timedelta(hours=1),
        [(exchange, btc, EntryDirection.DEBIT, "1", False), (exchange, btc, EntryDirection.DEBIT, "0.0002", True)],
    )
    destination = add_event(
        db_session,
        portfolio,
        LedgerEventType.TRANSFER_IN,
        start + timedelta(hours=1, minutes=2),
        [(wallet, btc, EntryDirection.CREDIT, "1", False)],
    )
    db_session.add(
        TransferGroup(
            reference="TRF_SEPARATE_FEE",
            portfolio_id=portfolio.id,
            source_event_id=source.id,
            destination_event_id=destination.id,
            source_account_id=exchange.id,
            destination_account_id=wallet.id,
            source_asset_id=btc.id,
            destination_asset_id=btc.id,
            source_amount=Decimal("1"),
            destination_amount=Decimal("1"),
            fee_amount=Decimal("0.0002"),
            fee_asset_id=btc.id,
            source_occurred_at=source.occurred_at,
            destination_occurred_at=destination.occurred_at,
            status=TransferGroupStatus.CONFIRMED,
            confidence_score=100,
            match_method="test",
            metadata_json={"internal_portfolio_transfer": True},
        )
    )
    db_session.commit()

    run = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest(method=CostMethod.FIFO))
    assert run.status == SyncRunStatus.SUCCEEDED, run.warnings_json
    source_position = db_session.scalar(
        select(PositionCostSnapshot).where(
            PositionCostSnapshot.run_id == run.id,
            PositionCostSnapshot.account_id == exchange.id,
            PositionCostSnapshot.asset_id == btc.id,
        )
    )
    wallet_position = db_session.scalar(
        select(PositionCostSnapshot).where(
            PositionCostSnapshot.run_id == run.id,
            PositionCostSnapshot.account_id == wallet.id,
            PositionCostSnapshot.asset_id == btc.id,
        )
    )
    assert source_position is None
    assert wallet_position.quantity == Decimal("1.000000000000000000")
    fee_record = db_session.scalar(
        select(RealizedPnlRecord).where(
            RealizedPnlRecord.run_id == run.id,
            RealizedPnlRecord.ledger_event_id == source.id,
            RealizedPnlRecord.category == "fee",
        )
    )
    assert fee_record.quantity == Decimal("0.000200000000000000")
    assert fee_record.cost_basis_usd is not None


def test_latest_balance_snapshot_can_zero_an_old_cost_lot(db_session):
    portfolio, exchange, _, btc, _, usdt, _ = seed_foundation(db_session)
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, start, [(exchange, usdt, EntryDirection.CREDIT, "100", False)])
    add_event(
        db_session,
        portfolio,
        LedgerEventType.BUY,
        start + timedelta(minutes=1),
        [(exchange, btc, EntryDirection.CREDIT, "1", False), (exchange, usdt, EntryDirection.DEBIT, "100", False)],
    )
    db_session.add(
        BalanceSnapshot(
            account_id=exchange.id,
            asset_id=btc.id,
            quantity=Decimal("0"),
            source="binance:spot",
            as_of=start + timedelta(minutes=2),
        )
    )
    db_session.commit()

    run = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest())
    assert run.status == SyncRunStatus.SUCCEEDED
    assert db_session.scalar(
        select(PositionCostSnapshot).where(
            PositionCostSnapshot.run_id == run.id,
            PositionCostSnapshot.account_id == exchange.id,
            PositionCostSnapshot.asset_id == btc.id,
        )
    ) is None


def test_fifo_lifo_and_average_are_rebuildable_from_the_same_ledger(db_session):
    portfolio, exchange, _, btc, _, usdt, _ = seed_foundation(db_session)
    start = datetime.now(timezone.utc) - timedelta(days=5)
    add_event(db_session, portfolio, LedgerEventType.DEPOSIT, start, [(exchange, usdt, EntryDirection.CREDIT, "300", False)])
    add_event(db_session, portfolio, LedgerEventType.BUY, start + timedelta(hours=1), [(exchange, btc, EntryDirection.CREDIT, "1", False), (exchange, usdt, EntryDirection.DEBIT, "100", False)])
    add_event(db_session, portfolio, LedgerEventType.BUY, start + timedelta(hours=2), [(exchange, btc, EntryDirection.CREDIT, "1", False), (exchange, usdt, EntryDirection.DEBIT, "200", False)])
    sale = add_event(db_session, portfolio, LedgerEventType.SELL, start + timedelta(hours=3), [(exchange, btc, EntryDirection.DEBIT, "1", False), (exchange, usdt, EntryDirection.CREDIT, "300", False)])

    expected = {
        CostMethod.FIFO: (Decimal("100"), Decimal("200")),
        CostMethod.LIFO: (Decimal("200"), Decimal("100")),
        CostMethod.AVERAGE_COST: (Decimal("150"), Decimal("150")),
    }
    for method, (cost, pnl) in expected.items():
        run = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest(method=method))
        assert run.status == SyncRunStatus.SUCCEEDED
        record = db_session.scalar(
            select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id, RealizedPnlRecord.ledger_event_id == sale.id, RealizedPnlRecord.category == "spot")
        )
        assert record.cost_basis_usd == cost
        assert record.realized_pnl_usd == pnl
        lots = list(db_session.scalars(select(CostLot).where(CostLot.run_id == run.id, CostLot.asset_id == btc.id)))
        assert len(lots) == 2


def test_unknown_deposit_can_use_event_and_position_overrides_with_market_price(db_session):
    portfolio, _, wallet, _, eth, _, _ = seed_foundation(db_session)
    occurred = datetime.now(timezone.utc) - timedelta(hours=2)
    deposit = add_event(db_session, portfolio, LedgerEventType.DEPOSIT, occurred, [(wallet, eth, EntryDirection.CREDIT, "2", False)])

    first = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest())
    assert first.status == SyncRunStatus.PARTIAL
    first_position = db_session.scalar(select(PositionCostSnapshot).where(PositionCostSnapshot.run_id == first.id, PositionCostSnapshot.asset_id == eth.id))
    assert first_position.calculated_cost_usd is None

    event_override = CostBasisOverride(
        portfolio_id=portfolio.id,
        account_id=wallet.id,
        asset_id=eth.id,
        ledger_event_id=deposit.id,
        override_type=CostOverrideType.EVENT_TOTAL,
        total_cost_usd=Decimal("1000"),
        reason="Known purchase cost",
    )
    position_override = CostBasisOverride(
        portfolio_id=portfolio.id,
        account_id=wallet.id,
        asset_id=eth.id,
        override_type=CostOverrideType.POSITION_TOTAL,
        total_cost_usd=Decimal("900"),
        reason="Reconciled statement cost",
    )
    db_session.add_all(
        [
            event_override,
            position_override,
            AssetPrice(asset_id=eth.id, price_usd=Decimal("800"), source="manual", as_of=datetime.now(timezone.utc) - timedelta(minutes=1)),
        ]
    )
    db_session.commit()
    second = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest())
    assert second.status == SyncRunStatus.SUCCEEDED
    position = db_session.scalar(select(PositionCostSnapshot).where(PositionCostSnapshot.run_id == second.id, PositionCostSnapshot.asset_id == eth.id))
    assert position.calculated_cost_usd == Decimal("1000")
    assert position.manual_cost_usd == Decimal("900")
    assert position.effective_cost_usd == Decimal("900")
    assert position.market_value_usd == Decimal("1600")
    assert position.unrealized_pnl_usd == Decimal("700")


def test_derivative_pnl_is_separate_from_spot_cost_lots(db_session):
    portfolio, exchange, _, _, _, _, usdc = seed_foundation(db_session)
    event = add_event(
        db_session,
        portfolio,
        LedgerEventType.SELL,
        datetime.now(timezone.utc) - timedelta(hours=1),
        [
            (exchange, usdc, EntryDirection.CREDIT, "100", False),
            (exchange, usdc, EntryDirection.DEBIT, "2", True),
        ],
        metadata={"market_type": "hyperliquid_perp", "closed_pnl": "100"},
    )
    run = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest())
    assert run.status == SyncRunStatus.SUCCEEDED
    record = db_session.scalar(select(RealizedPnlRecord).where(RealizedPnlRecord.run_id == run.id, RealizedPnlRecord.ledger_event_id == event.id))
    assert record.category == "derivative"
    assert record.realized_pnl_usd == Decimal("98")
    assert db_session.scalar(select(CostLot).where(CostLot.run_id == run.id, CostLot.asset_id == usdc.id)) is None


def test_usdt_and_usdc_market_values_do_not_require_price_rows(db_session):
    portfolio, exchange, _, _, _, usdt, usdc = seed_foundation(db_session)
    occurred_at = datetime.now(timezone.utc) - timedelta(hours=1)
    add_event(
        db_session,
        portfolio,
        LedgerEventType.DEPOSIT,
        occurred_at,
        [
            (exchange, usdt, EntryDirection.CREDIT, "100", False),
            (exchange, usdc, EntryDirection.CREDIT, "25", False),
        ],
    )
    db_session.execute(delete(AssetPrice))
    db_session.commit()

    run = CostBasisService(db_session).calculate(portfolio.id, CostBasisRunRequest(as_of=datetime.now(timezone.utc)))
    positions = {
        row.asset_id: row
        for row in db_session.scalars(select(PositionCostSnapshot).where(PositionCostSnapshot.run_id == run.id))
    }

    assert run.status == SyncRunStatus.SUCCEEDED
    assert positions[usdt.id].market_price_usd == Decimal("1")
    assert positions[usdt.id].market_value_usd == Decimal("100")
    assert positions[usdc.id].market_price_usd == Decimal("1")
    assert positions[usdc.id].market_value_usd == Decimal("25")


def test_cost_basis_http_api_calculates_and_protects_manual_inputs(client):
    portfolio = client.post("/api/v1/portfolios", json={"name": "HTTP Cost"}).json()
    account = client.post(
        "/api/v1/accounts",
        json={"portfolio_id": portfolio["id"], "kind": "wallet", "provider": "manual", "label": "Vault", "external_account_id": "vault-1"},
    ).json()
    asset = client.post(
        "/api/v1/assets",
        json={"canonical_symbol": "TEST", "name": "Test Asset", "asset_type": "token", "decimals": 18},
    ).json()
    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    event = client.post(
        "/api/v1/ledger/events",
        json={
            "portfolio_id": portfolio["id"],
            "event_type": "deposit",
            "source": "manual",
            "status": "posted",
            "occurred_at": occurred.isoformat(),
            "entries": [{"account_id": account["id"], "asset_id": asset["id"], "direction": "credit", "quantity": "2"}],
        },
    ).json()
    override = client.post(
        "/api/v1/cost-basis/overrides",
        json={
            "portfolio_id": portfolio["id"],
            "asset_id": asset["id"],
            "account_id": account["id"],
            "ledger_event_id": event["id"],
            "override_type": "event_total",
            "total_cost_usd": "100",
            "reason": "Statement evidence",
        },
    )
    assert override.status_code == 201, override.text
    price = client.post(
        "/api/v1/cost-basis/prices",
        json={"asset_id": asset["id"], "price_usd": "75", "source": "manual", "as_of": datetime.now(timezone.utc).isoformat()},
    )
    assert price.status_code == 201, price.text
    calculated = client.post(f"/api/v1/cost-basis/portfolios/{portfolio['id']}/calculate", json={"method": "average_cost"})
    assert calculated.status_code == 200, calculated.text
    summary = client.get(f"/api/v1/cost-basis/portfolios/{portfolio['id']}/assets")
    assert summary.status_code == 200, summary.text
    assert summary.json()[0]["calculated_cost_usd"] == "100.000000000000000000"
    assert summary.json()[0]["unrealized_pnl_usd"] == "50.000000000000000000"
