import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.connectors.perp_dex.hyperliquid.client import HyperliquidClient


def to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


@dataclass
class HistoryResult:
    records: list[dict[str, Any]]
    truncated: bool = False


class HyperliquidCollector:
    PAGE_SIZE = 2000

    def __init__(self, client: HyperliquidClient) -> None:
        self.client = client

    def clearinghouse_state(self, address: str) -> dict[str, Any]:
        response = self.client.info({"type": "clearinghouseState", "user": address})
        return response if isinstance(response, dict) else {}

    def spot_state(self, address: str) -> dict[str, Any]:
        response = self.client.info({"type": "spotClearinghouseState", "user": address})
        return response if isinstance(response, dict) else {}

    def perp_market_context(self) -> list[Any]:
        response = self.client.info({"type": "metaAndAssetCtxs"})
        return response if isinstance(response, list) else []

    def spot_meta(self) -> dict[str, Any]:
        response = self.client.info({"type": "spotMeta"})
        return response if isinstance(response, dict) else {}

    def fills(self, address: str, start: datetime, end: datetime) -> HistoryResult:
        return self._history(
            "userFillsByTime",
            address,
            start,
            end,
            max_records=10_000,
            extra={"aggregateByTime": False},
        )

    def funding(self, address: str, start: datetime, end: datetime) -> HistoryResult:
        return self._history("userFunding", address, start, end, max_records=100_000)

    def ledger_updates(self, address: str, start: datetime, end: datetime) -> HistoryResult:
        return self._history("userNonFundingLedgerUpdates", address, start, end, max_records=100_000)

    def _history(
        self,
        request_type: str,
        address: str,
        start: datetime,
        end: datetime,
        *,
        max_records: int,
        extra: dict[str, Any] | None = None,
    ) -> HistoryResult:
        cursor = to_ms(start)
        end_ms = to_ms(end)
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        truncated = False
        while cursor <= end_ms:
            payload: dict[str, Any] = {
                "type": request_type,
                "user": address,
                "startTime": cursor,
                "endTime": end_ms,
            }
            payload.update(extra or {})
            response = self.client.info(payload)
            if not isinstance(response, list) or not response:
                break
            page = [item for item in response if isinstance(item, dict)]
            new_records = 0
            for item in page:
                identity = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                if identity not in seen:
                    seen.add(identity)
                    records.append(item)
                    new_records += 1
            if len(records) >= max_records:
                records = records[:max_records]
                truncated = True
                break
            times = [int(item.get("time", cursor)) for item in page]
            next_cursor = max(times, default=cursor)
            if len(page) < self.PAGE_SIZE:
                break
            # The API paginates only by millisecond. Advancing to max(time)+1
            # can silently skip fills that share that final millisecond with a
            # full page. Re-query that timestamp and de-duplicate instead. If
            # it returns a full page with no new records, the API offers no
            # safe way to advance, so mark the collection incomplete and keep
            # the sync cursor unchanged.
            if next_cursor < cursor or new_records == 0:
                truncated = True
                break
            cursor = next_cursor
        return HistoryResult(records=records, truncated=truncated)
