from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class EvmChain:
    key: str
    chain_id: str
    name: str
    native_symbol: str
    native_name: str
    rpc_setting: str


CHAINS = {
    "ethereum": EvmChain("ethereum", "1", "Ethereum", "ETH", "Ether", "evm_ethereum_rpc_url"),
    "arbitrum": EvmChain("arbitrum", "42161", "Arbitrum", "ETH", "Ether", "evm_arbitrum_rpc_url"),
    "base": EvmChain("base", "8453", "Base", "ETH", "Ether", "evm_base_rpc_url"),
    "bsc": EvmChain("bsc", "56", "BNB Chain", "BNB", "BNB", "evm_bsc_rpc_url"),
    "optimism": EvmChain("optimism", "10", "Optimism", "ETH", "Ether", "evm_optimism_rpc_url"),
    "polygon": EvmChain("polygon", "137", "Polygon", "POL", "POL", "evm_polygon_rpc_url"),
}

ALIASES = {
    "1": "ethereum",
    "eth": "ethereum",
    "42161": "arbitrum",
    "arb": "arbitrum",
    "8453": "base",
    "56": "bsc",
    "bnb": "bsc",
    "bnb chain": "bsc",
    "10": "optimism",
    "op": "optimism",
    "137": "polygon",
    "matic": "polygon",
}


def resolve_chain(value: str | None) -> EvmChain | None:
    if not value:
        return None
    normalized = value.strip().lower()
    key = ALIASES.get(normalized, normalized)
    return CHAINS.get(key)


def rpc_url(chain: EvmChain, settings: Settings) -> str:
    value = getattr(settings, chain.rpc_setting, None)
    if not value:
        raise ValueError(f"{chain.rpc_setting.upper()} is not configured")
    return str(value).rstrip("/")
