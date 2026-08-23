from dataclasses import dataclass, field
from typing import Any

from app.connectors.evm.client import EvmRpcClient, EvmRpcError

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def hex_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(value)
        return int(value)
    except (TypeError, ValueError):
        return default


def hex_quantity(value: int) -> str:
    return hex(max(0, value))


def normalize_address(value: Any) -> str:
    return str(value or "").strip().lower()


def address_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def topic_address(value: Any) -> str:
    raw = str(value or "").lower().removeprefix("0x")
    return "0x" + raw[-40:] if len(raw) >= 40 else ""


def decode_abi_string(value: Any) -> str | None:
    raw = str(value or "").removeprefix("0x")
    if not raw:
        return None
    try:
        packed = bytes.fromhex(raw)
        if len(packed) == 32:
            decoded = packed.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
            return decoded or None
        if len(packed) >= 64:
            offset = int.from_bytes(packed[:32], "big")
            if offset + 32 <= len(packed):
                length = int.from_bytes(packed[offset : offset + 32], "big")
                decoded = packed[offset + 32 : offset + 32 + length].decode("utf-8", errors="ignore").strip()
                return decoded or None
    except (ValueError, UnicodeDecodeError):
        return None
    return None


@dataclass
class EvmCollection:
    transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    logs_by_transaction: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    token_contracts: set[str] = field(default_factory=set)
    failed_ranges: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EvmCollector:
    def __init__(self, client: EvmRpcClient, *, chunk_size: int = 5_000, native_scan_max_blocks: int = 2_000) -> None:
        self.client = client
        self.chunk_size = max(1, min(chunk_size, 10_000))
        self.native_scan_max_blocks = max(1, native_scan_max_blocks)

    def verified_latest_block(self, expected_chain_id: int, confirmations: int) -> tuple[int, int]:
        actual_chain_id = hex_int(self.client.call("eth_chainId"))
        if actual_chain_id != expected_chain_id:
            raise ValueError(f"RPC chain ID mismatch: expected {expected_chain_id}, received {actual_chain_id}")
        latest = hex_int(self.client.call("eth_blockNumber"))
        return latest, max(0, latest - max(0, confirmations))

    def collect(
        self,
        address: str,
        from_block: int,
        to_block: int,
        transaction_hashes: list[str],
    ) -> EvmCollection:
        result = EvmCollection()
        self._transfer_logs(result, address, from_block, to_block)
        hashes = {str(value).lower() for value in transaction_hashes}
        hashes.update(result.logs_by_transaction)
        self._native_transactions(result, address, from_block, to_block)
        hashes.update(result.transactions)
        self._hydrate_transactions(result, hashes)
        self._hydrate_receipts_and_blocks(result)
        return result

    def native_balance(self, address: str, block_number: int) -> int:
        return hex_int(self.client.call("eth_getBalance", [address, hex_quantity(block_number)]))

    def token_balance(self, contract: str, address: str, block_number: int) -> int:
        data = "0x70a08231" + address.removeprefix("0x").rjust(64, "0")
        return hex_int(self.client.call("eth_call", [{"to": contract, "data": data}, hex_quantity(block_number)]))

    def token_metadata(self, contract: str, block_number: int) -> tuple[str, str, int]:
        block = hex_quantity(block_number)
        symbol = decode_abi_string(self.client.call("eth_call", [{"to": contract, "data": "0x95d89b41"}, block]))
        name = decode_abi_string(self.client.call("eth_call", [{"to": contract, "data": "0x06fdde03"}, block]))
        decimals = hex_int(self.client.call("eth_call", [{"to": contract, "data": "0x313ce567"}, block]), 18)
        if decimals < 0 or decimals > 36:
            decimals = 18
        short = contract[:8].upper()
        return (symbol or short)[:32], (name or symbol or f"Token {short}")[:160], decimals

    def _transfer_logs(self, result: EvmCollection, address: str, from_block: int, to_block: int) -> None:
        wallet_topic = address_topic(address)
        seen: set[tuple[str, str]] = set()
        for start in range(from_block, to_block + 1, self.chunk_size):
            end = min(start + self.chunk_size - 1, to_block)
            filters = (
                ("outgoing", [TRANSFER_TOPIC, wallet_topic]),
                ("incoming", [TRANSFER_TOPIC, None, wallet_topic]),
            )
            for direction, topics in filters:
                try:
                    payload = self.client.call(
                        "eth_getLogs",
                        [{"fromBlock": hex_quantity(start), "toBlock": hex_quantity(end), "topics": topics}],
                    )
                except EvmRpcError as error:
                    result.failed_ranges.append(
                        {"kind": "erc20_logs", "direction": direction, "from_block": start, "to_block": end, "error": str(error)[:200]}
                    )
                    continue
                if not isinstance(payload, list):
                    continue
                for item in payload:
                    if not isinstance(item, dict) or item.get("removed") is True:
                        continue
                    topics_value = item.get("topics")
                    if not isinstance(topics_value, list) or len(topics_value) < 3 or str(topics_value[0]).lower() != TRANSFER_TOPIC:
                        continue
                    tx_hash = str(item.get("transactionHash") or "").lower()
                    log_index = str(item.get("logIndex") or "")
                    if not tx_hash or (tx_hash, log_index) in seen:
                        continue
                    seen.add((tx_hash, log_index))
                    contract = normalize_address(item.get("address"))
                    if contract:
                        result.token_contracts.add(contract)
                    result.logs_by_transaction.setdefault(tx_hash, []).append(item)

    def _native_transactions(self, result: EvmCollection, address: str, from_block: int, to_block: int) -> None:
        native_start = max(from_block, to_block - self.native_scan_max_blocks + 1)
        if native_start > from_block:
            result.warnings.append(
                f"Native-only transaction scan was limited to blocks {native_start}-{to_block}; backfill older ranges in chunks of at most {self.native_scan_max_blocks} blocks."
            )
        block_numbers = list(range(native_start, to_block + 1))
        for offset in range(0, len(block_numbers), 100):
            batch = block_numbers[offset : offset + 100]
            try:
                blocks = self.client.batch_call(
                    "eth_getBlockByNumber",
                    [[hex_quantity(block_number), True] for block_number in batch],
                )
            except EvmRpcError as error:
                result.warnings.append(f"RPC batch block read unavailable; falling back to individual reads: {str(error)[:140]}")
                blocks = []
                for block_number in batch:
                    try:
                        blocks.append(self.client.call("eth_getBlockByNumber", [hex_quantity(block_number), True]))
                    except EvmRpcError as item_error:
                        result.failed_ranges.append(
                            {"kind": "native_blocks", "from_block": block_number, "to_block": block_number, "error": str(item_error)[:200]}
                        )
                        blocks.append(None)
            for block_number, block in zip(batch, blocks, strict=True):
                if not isinstance(block, dict):
                    continue
                result.blocks[block_number] = block
                for transaction in block.get("transactions", []):
                    if not isinstance(transaction, dict):
                        continue
                    if address in {normalize_address(transaction.get("from")), normalize_address(transaction.get("to"))}:
                        tx_hash = str(transaction.get("hash") or "").lower()
                        if tx_hash:
                            result.transactions[tx_hash] = transaction

    def _hydrate_transactions(self, result: EvmCollection, hashes: set[str]) -> None:
        for tx_hash in sorted(hashes):
            if tx_hash in result.transactions:
                continue
            try:
                transaction = self.client.call("eth_getTransactionByHash", [tx_hash])
            except EvmRpcError as error:
                result.warnings.append(f"Transaction {tx_hash[:18]} could not be fetched: {str(error)[:140]}")
                result.failed_ranges.append(
                    {"kind": "transaction_hydration", "transaction_hash": tx_hash, "error": str(error)[:200]}
                )
                continue
            if isinstance(transaction, dict):
                result.transactions[tx_hash] = transaction

    def _hydrate_receipts_and_blocks(self, result: EvmCollection) -> None:
        for tx_hash, transaction in result.transactions.items():
            try:
                receipt = self.client.call("eth_getTransactionReceipt", [tx_hash])
            except EvmRpcError as error:
                result.warnings.append(f"Receipt {tx_hash[:18]} could not be fetched: {str(error)[:140]}")
                result.failed_ranges.append(
                    {"kind": "receipt_hydration", "transaction_hash": tx_hash, "error": str(error)[:200]}
                )
                continue
            if isinstance(receipt, dict):
                result.receipts[tx_hash] = receipt
            block_number = hex_int(transaction.get("blockNumber"))
            if block_number in result.blocks:
                continue
            try:
                block = self.client.call("eth_getBlockByNumber", [hex_quantity(block_number), False])
            except EvmRpcError as error:
                result.warnings.append(f"Block {block_number} could not be fetched: {str(error)[:140]}")
                result.failed_ranges.append(
                    {"kind": "block_hydration", "from_block": block_number, "to_block": block_number, "error": str(error)[:200]}
                )
                continue
            if isinstance(block, dict):
                result.blocks[block_number] = block
