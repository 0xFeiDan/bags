from dataclasses import dataclass, field
from collections.abc import Callable

from app.connectors.zerion.client import ZerionApiClient, ZerionPage


@dataclass
class ZerionShadowCollection:
    recent_transactions: ZerionPage
    simple_positions: ZerionPage | None = None
    backfill_transactions: ZerionPage | None = None
    next_backfill_url: str | None = None
    backfill_complete: bool = False
    warnings: list[str] = field(default_factory=list)


class ZerionShadowCollector:
    def __init__(self, client: ZerionApiClient) -> None:
        self.client = client

    def collect(
        self,
        address: str,
        chain_id: str,
        *,
        backfill_url: str | None,
        backfill_complete: bool,
        on_page: Callable[[str, ZerionPage], None] | None = None,
    ) -> ZerionShadowCollection:
        recent = self.client.wallet_transactions(address, chain_id)
        if on_page:
            on_page("transactions", recent)
        result = ZerionShadowCollection(recent_transactions=recent, backfill_complete=backfill_complete)

        if self.client.remaining_request_budget > 0:
            result.simple_positions = self.client.wallet_simple_positions(address, chain_id)
            if on_page:
                on_page("positions", result.simple_positions)
            if result.simple_positions.next_url:
                result.warnings.append("Simple positions exceed one page; Phase 2 stores the first 100 only.")
        else:
            result.warnings.append("The per-run budget ended before simple positions could be collected.")

        candidate = None if backfill_complete else (backfill_url or recent.next_url)
        if candidate and self.client.remaining_request_budget > 0:
            result.backfill_transactions = self.client.wallet_transactions(address, chain_id, url=candidate)
            if on_page:
                on_page("transactions", result.backfill_transactions)
            result.next_backfill_url = result.backfill_transactions.next_url
            result.backfill_complete = result.next_backfill_url is None
        elif candidate:
            result.next_backfill_url = candidate
            result.warnings.append("Transaction backfill is pending because the per-run budget was exhausted.")
        elif not backfill_complete:
            result.backfill_complete = True
        return result
