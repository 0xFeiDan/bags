import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from app.connectors.evm.client import EvmRpcClient
from app.connectors.evm.collector import TRANSFER_TOPIC
from app.connectors.evm.sync import EvmWalletSyncService
from app.core.config import Settings
from app.models import (
    Account,
    AccountKind,
    Asset,
    BalanceSnapshot,
    EntryDirection,
    LedgerEntry,
    LedgerEvent,
    Portfolio,
    RawEvent,
    SyncRunStatus,
)
from app.schemas import EvmSyncRequest

WALLET = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
TOKEN = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HASH_IN_TOKEN = "0x" + "01" * 32
HASH_OUT_TOKEN = "0x" + "02" * 32
HASH_IN_NATIVE = "0x" + "03" * 32
HASH_OUT_NATIVE = "0x" + "04" * 32
HASH_FAILED = "0x" + "05" * 32


def topic(address: str) -> str:
    return "0x" + address.removeprefix("0x").rjust(64, "0")


def transfer_log(tx_hash: str, block: int, sender: str, recipient: str, amount: int, index: int = 0):
    return {
        "address": TOKEN,
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
        "logIndex": hex(index),
        "data": hex(amount),
        "topics": [TRANSFER_TOPIC, topic(sender), topic(recipient)],
        "removed": False,
    }


TOKEN_IN_LOG = transfer_log(HASH_IN_TOKEN, 990, OTHER, WALLET, 100 * 10**18)
TOKEN_OUT_LOG = transfer_log(HASH_OUT_TOKEN, 995, WALLET, OTHER, 40 * 10**18)


def transaction(tx_hash: str, block: int, sender: str, recipient: str, value: int = 0):
    return {
        "hash": tx_hash,
        "blockNumber": hex(block),
        "from": sender,
        "to": recipient,
        "value": hex(value),
        "gasPrice": hex(1_000_000_000),
    }


TRANSACTIONS = {
    HASH_IN_TOKEN: transaction(HASH_IN_TOKEN, 990, OTHER, TOKEN),
    HASH_OUT_TOKEN: transaction(HASH_OUT_TOKEN, 995, WALLET, TOKEN),
    HASH_IN_NATIVE: transaction(HASH_IN_NATIVE, 998, OTHER, WALLET, 10**18),
    HASH_OUT_NATIVE: transaction(HASH_OUT_NATIVE, 999, WALLET, OTHER, 2 * 10**17),
    HASH_FAILED: transaction(HASH_FAILED, 1000, WALLET, OTHER, 5 * 10**17),
}


class FakeEvmClient:
    fail_outgoing_logs = False

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def batch_call(self, method, params_list):
        return [self.call(method, params) for params in params_list]

    def call(self, method, params=None):
        params = params or []
        if method == "eth_chainId":
            return "0x38"
        if method == "eth_blockNumber":
            return hex(1000)
        if method == "eth_getLogs":
            topics = params[0]["topics"]
            if len(topics) == 2:
                if type(self).fail_outgoing_logs:
                    from app.connectors.evm.client import EvmRpcError

                    raise EvmRpcError("range unavailable", method=method, code=-32005)
                return [TOKEN_OUT_LOG]
            return [TOKEN_IN_LOG]
        if method == "eth_getBlockByNumber":
            block_number = int(params[0], 16)
            full = bool(params[1])
            transactions = []
            if full:
                transactions = [item for item in TRANSACTIONS.values() if int(item["blockNumber"], 16) == block_number]
            return {"number": hex(block_number), "timestamp": hex(1_700_000_000 + block_number), "transactions": transactions}
        if method == "eth_getTransactionByHash":
            return TRANSACTIONS.get(params[0])
        if method == "eth_getTransactionReceipt":
            tx_hash = params[0]
            logs = [TOKEN_IN_LOG] if tx_hash == HASH_IN_TOKEN else [TOKEN_OUT_LOG] if tx_hash == HASH_OUT_TOKEN else []
            return {
                "transactionHash": tx_hash,
                "status": "0x0" if tx_hash == HASH_FAILED else "0x1",
                "gasUsed": hex(21_000),
                "effectiveGasPrice": hex(1_000_000_000),
                "logs": logs,
            }
        if method == "eth_getBalance":
            return hex(10 * 10**18)
        if method == "eth_call":
            data = params[0]["data"]
            if data.startswith("0x70a08231"):
                return hex(60 * 10**18)
            if data == "0x95d89b41":
                return "0x" + b"USDT".ljust(32, b"\x00").hex()
            if data == "0x06fdde03":
                return "0x" + b"Test USD".ljust(32, b"\x00").hex()
            if data == "0x313ce567":
                return hex(18)
        raise AssertionError(f"unexpected RPC method: {method} {params}")


def seed_wallet(db_session):
    portfolio = Portfolio(name="EVM Portfolio")
    db_session.add(portfolio)
    db_session.flush()
    account = Account(
        portfolio_id=portfolio.id,
        kind=AccountKind.WALLET,
        provider="evm",
        label="BNB Wallet",
        external_account_id=WALLET,
        address=WALLET,
        chain_id="56",
    )
    db_session.add(account)
    db_session.commit()
    return account


def test_rpc_client_hard_blocks_mutable_methods():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": "0x1"})

    with EvmRpcClient(base_url="https://rpc.test", transport=httpx.MockTransport(handler)) as client:
        assert client.call("eth_chainId") == "0x1"
        try:
            client.call("eth_sendRawTransaction", ["0xdead"])
            raise AssertionError("mutable method should have been rejected")
        except ValueError:
            pass
    assert seen == ["eth_chainId"]


def test_evm_sync_persists_raw_receipts_balances_gas_and_is_idempotent(db_session):
    account = seed_wallet(db_session)
    settings = Settings(
        evm_bsc_rpc_url="https://bsc.test",
        evm_confirmations=0,
        evm_native_scan_max_blocks=2_000,
    )
    request = EvmSyncRequest(from_block=990, to_block=1000)
    FakeEvmClient.fail_outgoing_logs = False

    first = EvmWalletSyncService(db_session, settings, client_factory=FakeEvmClient).run(account.id, request)
    assert first.status == SyncRunStatus.SUCCEEDED, (first.error_code, first.error_message, first.warnings_json)
    assert first.failed_ranges_json == []
    assert first.stats_json == {
        "raw_created": 6,
        "raw_existing": 0,
        "ledger_created": 5,
        "balances_created": 2,
        "transactions_seen": 5,
        "token_contracts_seen": 1,
    }
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 6
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 5
    assert db_session.scalar(select(func.count()).select_from(BalanceSnapshot)) == 2

    token_asset = db_session.scalar(select(Asset).where(Asset.contract_address == TOKEN))
    assert token_asset.canonical_symbol == "USDT"
    assert token_asset.decimals == 18
    token_balance = db_session.scalar(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.asset_id == token_asset.id)
    )
    assert token_balance.quantity == Decimal("60")
    gas_entries = list(db_session.scalars(select(LedgerEntry).where(LedgerEntry.fee_flag.is_(True))))
    assert len(gas_entries) == 3
    assert all(item.direction == EntryDirection.DEBIT for item in gas_entries)
    assert sum((item.quantity for item in gas_entries), Decimal("0")) == Decimal("0.000063")
    native_asset = db_session.scalar(select(Asset).where(Asset.chain_id == "56", Asset.contract_address.is_(None)))
    native_value_debits = list(
        db_session.scalars(
            select(LedgerEntry).where(
                LedgerEntry.asset_id == native_asset.id,
                LedgerEntry.direction == EntryDirection.DEBIT,
                LedgerEntry.fee_flag.is_(False),
            )
        )
    )
    assert abs(sum((item.quantity for item in native_value_debits), Decimal("0")) - Decimal("0.2")) < Decimal("0.000000000000001")

    second = EvmWalletSyncService(db_session, settings, client_factory=FakeEvmClient).run(account.id, request)
    assert second.status == SyncRunStatus.SUCCEEDED
    assert second.stats_json["raw_created"] == 0
    assert second.stats_json["raw_existing"] == 6
    assert second.stats_json["ledger_created"] == 0
    assert second.stats_json["balances_created"] == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEvent)) == 5


def test_failed_log_range_is_reported_as_partial(db_session):
    account = seed_wallet(db_session)
    settings = Settings(evm_bsc_rpc_url="https://bsc.test", evm_confirmations=0)
    FakeEvmClient.fail_outgoing_logs = True
    run = EvmWalletSyncService(db_session, settings, client_factory=FakeEvmClient).run(
        account.id,
        EvmSyncRequest(from_block=990, to_block=1000),
    )
    FakeEvmClient.fail_outgoing_logs = False
    assert run.status == SyncRunStatus.PARTIAL
    assert run.failed_ranges_json[0]["kind"] == "erc20_logs"
    assert run.failed_ranges_json[0]["from_block"] == 990


def test_evm_wallet_account_api_normalizes_chain_and_rejects_secret_fields(client):
    portfolio = client.post("/api/v1/portfolios", json={"name": "Wallet API", "base_currency": "USD"}).json()
    created = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "wallet",
            "provider": "evm",
            "label": "Base Wallet",
            "chain_id": "base",
            "address": WALLET.upper().replace("0X", "0x"),
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["chain_id"] == "8453"
    assert created.json()["address"] == WALLET
    assert created.json()["external_account_id"] == f"8453:{WALLET}"

    same_address_other_chain = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "wallet",
            "provider": "evm",
            "label": "BSC Wallet",
            "chain_id": "bsc",
            "address": WALLET,
        },
    )
    assert same_address_other_chain.status_code == 201, same_address_other_chain.text
    assert same_address_other_chain.json()["external_account_id"] == f"56:{WALLET}"

    rejected = client.post(
        "/api/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "kind": "wallet",
            "provider": "evm",
            "label": "Unsafe",
            "chain_id": "base",
            "address": "0x3333333333333333333333333333333333333333",
            "private_key": "must-never-be-accepted",
        },
    )
    assert rejected.status_code == 422
