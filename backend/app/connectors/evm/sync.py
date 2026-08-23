import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.evm.chains import EvmChain, resolve_chain, rpc_url
from app.connectors.evm.client import EvmRpcClient, EvmRpcError
from app.connectors.evm.collector import (
    TRANSFER_TOPIC,
    EvmCollection,
    EvmCollector,
    hex_int,
    normalize_address,
    topic_address,
)
from app.core.config import Settings
from app.models import (
    Account,
    AccountKind,
    Asset,
    AssetAlias,
    AssetType,
    BalanceSnapshot,
    EntryDirection,
    EventSource,
    EventStatus,
    EvmTrackedContract,
    LedgerEntry,
    LedgerEvent,
    LedgerEventType,
    RawEvent,
    RawEventStatus,
    SyncCursor,
    SyncRunStatus,
    WalletSyncRun,
    utc_now,
)
from app.schemas import EvmSyncRequest

ADDRESS_PATTERN = re.compile(r"^0x[a-f0-9]{40}$")


class EvmSyncStats:
    def __init__(self) -> None:
        self.raw_created = 0
        self.raw_existing = 0
        self.ledger_created = 0
        self.balances_created = 0
        self.transactions_seen = 0
        self.token_contracts_seen = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_created": self.raw_created,
            "raw_existing": self.raw_existing,
            "ledger_created": self.ledger_created,
            "balances_created": self.balances_created,
            "transactions_seen": self.transactions_seen,
            "token_contracts_seen": self.token_contracts_seen,
        }


class EvmWalletSyncService:
    def __init__(self, session: Session, settings: Settings, *, client_factory: type[EvmRpcClient] = EvmRpcClient) -> None:
        self.session = session
        self.settings = settings
        self.client_factory = client_factory
        self.stats = EvmSyncStats()
        self.warnings: list[str] = []
        self._asset_cache: dict[str, Asset] = {}
        self._metadata_cache: dict[str, tuple[str, str, int]] = {}

    def run(self, account_id: UUID, request: EvmSyncRequest) -> WalletSyncRun:
        account = self.session.get(Account, account_id)
        chain, address = self._validate_account(account)
        run = WalletSyncRun(account_id=account.id, chain_id=chain.chain_id)
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        try:
            with self.client_factory(
                base_url=rpc_url(chain, self.settings),
                timeout_seconds=self.settings.evm_request_timeout_seconds,
                max_retries=self.settings.evm_max_retries,
            ) as client:
                collector = EvmCollector(
                    client,
                    chunk_size=self.settings.evm_log_chunk_size,
                    native_scan_max_blocks=self.settings.evm_native_scan_max_blocks,
                )
                latest, confirmed = collector.verified_latest_block(int(chain.chain_id), self.settings.evm_confirmations)
                from_block, to_block = self._block_range(account.id, chain, request, confirmed)
                run = self.session.get(WalletSyncRun, run.id)
                run.from_block = from_block
                run.to_block = to_block
                run.latest_confirmed_block = confirmed
                self.session.commit()

                collection = collector.collect(address, from_block, to_block, request.transaction_hashes)
                self.warnings.extend(collection.warnings)
                self.stats.transactions_seen = len(collection.transactions)
                known_contracts = {
                    str(value).lower()
                    for value in self.session.scalars(
                        select(Asset.contract_address).where(
                            Asset.id.in_(select(BalanceSnapshot.asset_id).where(BalanceSnapshot.account_id == account.id)),
                            Asset.chain_id == chain.chain_id,
                            Asset.contract_address.is_not(None),
                        )
                    )
                    if value
                }
                requested_contracts = {contract.lower() for contract in request.token_contracts}
                tracked_contracts = {
                    contract.lower()
                    for contract in self.session.scalars(
                        select(EvmTrackedContract.contract_address).where(
                            EvmTrackedContract.account_id == account.id,
                            EvmTrackedContract.is_active.is_(True),
                        )
                    )
                }
                discovered_contracts = {contract.lower() for contract in collection.token_contracts}
                ordered_contracts: list[str] = []
                for group in (
                    sorted(requested_contracts),
                    sorted(tracked_contracts),
                    sorted(known_contracts),
                    sorted(discovered_contracts),
                ):
                    for contract in group:
                        if contract not in ordered_contracts:
                            ordered_contracts.append(contract)
                if len(ordered_contracts) > self.settings.evm_max_token_contracts:
                    self.warnings.append(
                        f"Token balance checks were capped at {self.settings.evm_max_token_contracts} contracts; request bounded token_contracts to inspect the remainder."
                    )
                contracts = set(ordered_contracts[: self.settings.evm_max_token_contracts])
                native_scan_complete = to_block - from_block + 1 <= self.settings.evm_native_scan_max_blocks
                if not native_scan_complete:
                    self.warnings.append(
                        "Native transaction scan is incomplete for this range; the incremental cursor was not advanced."
                    )
                self.stats.token_contracts_seen = len(contracts)

                for tx_hash in sorted(collection.transactions):
                    self._persist_transaction(account, chain, address, tx_hash, collection, collector, to_block)
                self._sync_balances(account, chain, address, to_block, contracts, collector, collection)
                if not collection.failed_ranges and native_scan_complete and request.from_block is None and request.to_block is None:
                    self._update_cursor(account.id, chain, to_block)
                self.session.commit()

                status = SyncRunStatus.PARTIAL if self.warnings or collection.failed_ranges else SyncRunStatus.SUCCEEDED
                self._finish(run.id, status, collection.failed_ranges)
                return self.session.get(WalletSyncRun, run.id)  # type: ignore[return-value]
        except (ValueError, EvmRpcError) as error:
            self.session.rollback()
            code = "EVM_RPC_ERROR" if isinstance(error, EvmRpcError) else "EVM_VALIDATION_ERROR"
            self._finish(run.id, SyncRunStatus.FAILED, [], code, str(error)[:300])
            return self.session.get(WalletSyncRun, run.id)  # type: ignore[return-value]
        except Exception as error:
            self.session.rollback()
            self._finish(run.id, SyncRunStatus.FAILED, [], "EVM_SYNC_ERROR", str(error)[:300])
            return self.session.get(WalletSyncRun, run.id)  # type: ignore[return-value]

    def _validate_account(self, account: Account | None) -> tuple[EvmChain, str]:
        if not account or not account.is_active:
            raise ValueError("wallet account not found or disabled")
        if account.kind != AccountKind.WALLET or account.provider != "evm":
            raise ValueError("EVM sync requires an active wallet account with provider evm")
        chain = resolve_chain(account.chain_id)
        if not chain:
            raise ValueError("unsupported EVM chain")
        address = normalize_address(account.address or account.external_account_id)
        if not ADDRESS_PATTERN.fullmatch(address):
            raise ValueError("wallet account requires a valid public EVM address")
        return chain, address

    def _block_range(self, account_id: UUID, chain: EvmChain, request: EvmSyncRequest, confirmed: int) -> tuple[int, int]:
        to_block = request.to_block if request.to_block is not None else confirmed
        if to_block > confirmed:
            raise ValueError(f"to_block exceeds latest confirmed block {confirmed}")
        if request.from_block is not None:
            from_block = request.from_block
        else:
            cursor = self.session.scalar(
                select(SyncCursor).where(SyncCursor.account_id == account_id, SyncCursor.resource == f"evm:{chain.chain_id}:blocks")
            )
            if cursor and cursor.cursor_value:
                try:
                    from_block = int(cursor.cursor_value) + 1
                except ValueError:
                    from_block = max(0, to_block - self.settings.evm_default_lookback_blocks + 1)
            else:
                from_block = max(0, to_block - self.settings.evm_default_lookback_blocks + 1)
        if from_block > to_block:
            from_block = to_block
        if to_block - from_block + 1 > self.settings.evm_max_block_span:
            raise ValueError(f"a single EVM sync cannot exceed {self.settings.evm_max_block_span} blocks")
        return from_block, to_block

    def _persist_transaction(
        self,
        account: Account,
        chain: EvmChain,
        address: str,
        tx_hash: str,
        collection: EvmCollection,
        collector: EvmCollector,
        metadata_block: int,
    ) -> None:
        transaction = collection.transactions[tx_hash]
        receipt = collection.receipts.get(tx_hash, {})
        block_number = hex_int(transaction.get("blockNumber"))
        block = collection.blocks.get(block_number, {})
        occurred_at = self._block_time(block)
        relevant_logs = self._relevant_transfer_logs(
            address,
            collection.logs_by_transaction.get(tx_hash, []),
            receipt.get("logs", []) if isinstance(receipt.get("logs"), list) else [],
        )
        payload = {
            "chain_id": chain.chain_id,
            "chain": chain.key,
            "transaction": transaction,
            "receipt": receipt,
            "transfer_logs": relevant_logs,
        }
        raw, _ = self._raw(account, chain, tx_hash, "evm_transaction", occurred_at, payload)
        if self.session.scalar(select(LedgerEvent.id).where(LedgerEvent.raw_event_id == raw.id)):
            return

        if not receipt:
            raw.status = RawEventStatus.FAILED
            self.warnings.append(f"Transaction {tx_hash[:18]} was stored but not normalized because its receipt is unavailable.")
            return

        legs: list[tuple[Asset, EntryDirection, Decimal, bool, dict[str, Any]]] = []
        native = self._native_asset(chain)
        transaction_succeeded = hex_int(receipt.get("status"), -1) == 1
        value = self._units(hex_int(transaction.get("value")), 18)
        sender = normalize_address(transaction.get("from"))
        recipient = normalize_address(transaction.get("to"))
        if transaction_succeeded and value > 0 and sender == address:
            legs.append((native, EntryDirection.DEBIT, value, False, {"component": "native_value"}))
        if transaction_succeeded and value > 0 and recipient == address:
            legs.append((native, EntryDirection.CREDIT, value, False, {"component": "native_value"}))

        for log in relevant_logs if transaction_succeeded else []:
            topics = log.get("topics") if isinstance(log.get("topics"), list) else []
            if len(topics) < 3:
                continue
            contract = normalize_address(log.get("address"))
            asset = self._token_asset(chain, contract, collector, metadata_block)
            quantity = self._units(hex_int(log.get("data")), asset.decimals)
            if quantity <= 0:
                continue
            log_meta = {"component": "erc20_transfer", "contract": contract, "log_index": log.get("logIndex")}
            if topic_address(topics[1]) == address:
                legs.append((asset, EntryDirection.DEBIT, quantity, False, log_meta))
            if topic_address(topics[2]) == address:
                legs.append((asset, EntryDirection.CREDIT, quantity, False, log_meta))

        gas_used = hex_int(receipt.get("gasUsed"))
        gas_price = hex_int(receipt.get("effectiveGasPrice") or transaction.get("gasPrice"))
        gas_fee = self._units(gas_used * gas_price, 18)
        if sender == address and gas_fee > 0:
            legs.append((native, EntryDirection.DEBIT, gas_fee, True, {"component": "gas", "gas_used": gas_used, "gas_price_wei": gas_price}))

        if not legs:
            raw.status = RawEventStatus.IGNORED
            return
        event_type = self._event_type(legs)
        event = LedgerEvent(
            portfolio_id=account.portfolio_id,
            raw_event_id=raw.id,
            event_type=event_type,
            source=EventSource.RAW,
            status=EventStatus.POSTED,
            occurred_at=occurred_at,
            tx_hash=tx_hash,
            external_reference=f"{chain.chain_id}:{tx_hash}",
            metadata_json={
                "chain_id": chain.chain_id,
                "chain": chain.key,
                "block_number": block_number,
                "transaction_status": hex_int(receipt.get("status"), -1),
                "gas_fee_native": str(gas_fee),
            },
        )
        self.session.add(event)
        self.session.flush()
        for asset, direction, quantity, fee_flag, metadata in legs:
            self.session.add(
                LedgerEntry(
                    ledger_event_id=event.id,
                    account_id=account.id,
                    asset_id=asset.id,
                    direction=direction,
                    quantity=quantity,
                    fee_flag=fee_flag,
                    metadata_json=metadata,
                )
            )
        raw.status = RawEventStatus.NORMALIZED
        self.stats.ledger_created += 1

    def _sync_balances(
        self,
        account: Account,
        chain: EvmChain,
        address: str,
        block_number: int,
        contracts: set[str],
        collector: EvmCollector,
        collection: EvmCollection,
    ) -> None:
        block = collection.blocks.get(block_number)
        if not block:
            value = collector.client.call("eth_getBlockByNumber", [hex(block_number), False])
            block = value if isinstance(value, dict) else {}
        as_of = self._block_time(block)
        balances_payload: list[dict[str, Any]] = []
        native = self._native_asset(chain)
        native_quantity = self._units(collector.native_balance(address, block_number), 18)
        self._balance(account.id, native.id, native_quantity, f"evm:{chain.chain_id}", as_of)
        balances_payload.append({"asset": native.canonical_symbol, "quantity": str(native_quantity), "contract": None})

        for contract in sorted(contracts):
            if not ADDRESS_PATTERN.fullmatch(contract):
                self.warnings.append(f"Invalid token contract skipped: {contract[:80]}")
                continue
            try:
                asset = self._token_asset(chain, contract, collector, block_number)
                quantity = self._units(collector.token_balance(contract, address, block_number), asset.decimals)
            except EvmRpcError as error:
                self.warnings.append(f"Token balance failed for {contract}: {str(error)[:160]}")
                continue
            self._balance(account.id, asset.id, quantity, f"evm:{chain.chain_id}", as_of)
            balances_payload.append({"asset": asset.canonical_symbol, "quantity": str(quantity), "contract": contract})

        raw, _ = self._raw(
            account,
            chain,
            f"balance:{block_number}",
            "evm_balance_snapshot",
            as_of,
            {"chain_id": chain.chain_id, "block_number": block_number, "address": address, "balances": balances_payload},
        )
        raw.status = RawEventStatus.NORMALIZED

    def _native_asset(self, chain: EvmChain) -> Asset:
        key = f"native:{chain.chain_id}"
        if key in self._asset_cache:
            return self._asset_cache[key]
        asset = self.session.scalar(
            select(Asset).where(
                Asset.canonical_symbol == chain.native_symbol,
                Asset.chain_id == chain.chain_id,
                Asset.contract_address.is_(None),
            )
        )
        if not asset:
            asset = Asset(
                canonical_symbol=chain.native_symbol,
                name=chain.native_name,
                asset_type=AssetType.NATIVE,
                decimals=18,
                chain_id=chain.chain_id,
                contract_address=None,
            )
            self.session.add(asset)
            self.session.flush()
        self._asset_cache[key] = asset
        return asset

    def _token_asset(self, chain: EvmChain, contract: str, collector: EvmCollector, block_number: int) -> Asset:
        contract = contract.lower()
        key = f"token:{chain.chain_id}:{contract}"
        if key in self._asset_cache:
            return self._asset_cache[key]
        asset = self.session.scalar(
            select(Asset).where(Asset.chain_id == chain.chain_id, Asset.contract_address == contract)
        )
        if not asset:
            try:
                metadata = self._metadata_cache.get(contract) or collector.token_metadata(contract, block_number)
                self._metadata_cache[contract] = metadata
            except EvmRpcError as error:
                self.warnings.append(f"Token metadata failed for {contract}; using safe fallback: {str(error)[:120]}")
                metadata = (contract[:8].upper(), f"Unknown token {contract[:10]}", 18)
            symbol, name, decimals = metadata
            asset = Asset(
                canonical_symbol=symbol,
                name=name,
                asset_type=AssetType.TOKEN,
                decimals=decimals,
                chain_id=chain.chain_id,
                contract_address=contract,
            )
            self.session.add(asset)
            self.session.flush()
        alias_source = f"evm:{chain.chain_id}"
        alias = self.session.scalar(
            select(AssetAlias).where(AssetAlias.source == alias_source, AssetAlias.source_asset_id == contract)
        )
        if not alias:
            self.session.add(
                AssetAlias(
                    asset_id=asset.id,
                    source=alias_source,
                    source_asset_id=contract,
                    symbol=asset.canonical_symbol,
                    metadata_json={"chain_id": chain.chain_id, "decimals": asset.decimals},
                )
            )
            self.session.flush()
        self._asset_cache[key] = asset
        return asset

    def _raw(
        self,
        account: Account,
        chain: EvmChain,
        external_id: str,
        event_kind: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> tuple[RawEvent, bool]:
        source = f"evm:{chain.chain_id}"
        existing = self.session.scalar(
            select(RawEvent).where(
                RawEvent.account_id == account.id,
                RawEvent.source == source,
                RawEvent.external_event_id == external_id,
            )
        )
        if existing:
            self.stats.raw_existing += 1
            return existing, False
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        raw = RawEvent(
            account_id=account.id,
            connection_id=None,
            source=source,
            external_event_id=external_id[:256],
            event_kind=event_kind,
            occurred_at=occurred_at,
            payload_json=payload,
            payload_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            status=RawEventStatus.RECEIVED,
        )
        self.session.add(raw)
        self.session.flush()
        self.stats.raw_created += 1
        return raw, True

    def _balance(self, account_id: UUID, asset_id: UUID, quantity: Decimal, source: str, as_of: datetime) -> None:
        existing = self.session.scalar(
            select(BalanceSnapshot.id).where(
                BalanceSnapshot.account_id == account_id,
                BalanceSnapshot.asset_id == asset_id,
                BalanceSnapshot.as_of == as_of,
            )
        )
        if existing:
            return
        self.session.add(
            BalanceSnapshot(
                account_id=account_id,
                asset_id=asset_id,
                quantity=quantity,
                source=source,
                as_of=as_of,
            )
        )
        self.stats.balances_created += 1

    def _update_cursor(self, account_id: UUID, chain: EvmChain, block_number: int) -> None:
        resource = f"evm:{chain.chain_id}:blocks"
        cursor = self.session.scalar(
            select(SyncCursor).where(SyncCursor.account_id == account_id, SyncCursor.resource == resource)
        )
        if not cursor:
            cursor = SyncCursor(account_id=account_id, resource=resource)
            self.session.add(cursor)
        cursor.cursor_value = str(block_number)
        cursor.last_synced_at = utc_now()

    def _finish(
        self,
        run_id: UUID,
        status: SyncRunStatus,
        failed_ranges: list[dict[str, Any]],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        run = self.session.get(WalletSyncRun, run_id)
        if not run:
            return
        run.status = status
        run.stats_json = self.stats.as_dict()
        run.warnings_json = self.warnings
        run.failed_ranges_json = failed_ranges
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = utc_now()
        self.session.commit()

    @staticmethod
    def _block_time(block: dict[str, Any]) -> datetime:
        timestamp = hex_int(block.get("timestamp"))
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp > 0 else utc_now()
        except (OSError, OverflowError, ValueError):
            return utc_now()

    @staticmethod
    def _units(raw: int, decimals: int) -> Decimal:
        return Decimal(raw) / (Decimal(10) ** max(0, min(decimals, 36)))

    @staticmethod
    def _event_type(legs: list[tuple[Asset, EntryDirection, Decimal, bool, dict[str, Any]]]) -> LedgerEventType:
        non_fee = [leg for leg in legs if not leg[3]]
        has_credit = any(leg[1] == EntryDirection.CREDIT for leg in non_fee)
        has_debit = any(leg[1] == EntryDirection.DEBIT for leg in non_fee)
        if has_credit and has_debit:
            return LedgerEventType.SWAP
        if has_credit:
            return LedgerEventType.TRANSFER_IN
        if has_debit:
            return LedgerEventType.TRANSFER_OUT
        return LedgerEventType.FEE

    @staticmethod
    def _relevant_transfer_logs(address: str, primary: list[Any], receipt_logs: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in [*primary, *receipt_logs]:
            if not isinstance(item, dict) or item.get("removed") is True:
                continue
            topics = item.get("topics")
            if not isinstance(topics, list) or len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                continue
            if address not in {topic_address(topics[1]), topic_address(topics[2])}:
                continue
            identity = (str(item.get("transactionHash") or ""), str(item.get("logIndex") or ""))
            if identity in seen:
                continue
            seen.add(identity)
            result.append(item)
        return result
