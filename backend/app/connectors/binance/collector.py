from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from app.connectors.binance.client import BinanceApiClient, BinanceProduct


def to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def time_windows(start: datetime, end: datetime, span: timedelta) -> Iterator[tuple[int, int]]:
    cursor = start
    while cursor < end:
        window_end = min(cursor + span - timedelta(milliseconds=1), end)
        yield to_ms(cursor), to_ms(window_end)
        cursor = window_end + timedelta(milliseconds=1)


class BinanceCollector:
    """Endpoint-specific read pagination; contains no persistence logic."""

    def __init__(self, client: BinanceApiClient) -> None:
        self.client = client

    def exchange_info(self, product: BinanceProduct) -> dict[str, Any]:
        path = {"spot": "/api/v3/exchangeInfo", "usdm": "/fapi/v1/exchangeInfo", "coinm": "/dapi/v1/exchangeInfo"}[product]
        response = self.client.public_get(product, path)
        return response if isinstance(response, dict) else {}

    def account(self, product: BinanceProduct) -> dict[str, Any]:
        path = {"spot": "/api/v3/account", "usdm": "/fapi/v3/account", "coinm": "/dapi/v1/account"}[product]
        response = self.client.signed_get(product, path)
        return response if isinstance(response, dict) else {}

    def api_restrictions(self) -> dict[str, Any]:
        response = self.client.signed_get("spot", "/sapi/v1/account/apiRestrictions")
        return response if isinstance(response, dict) else {}

    def positions(self, product: BinanceProduct) -> list[dict[str, Any]]:
        if product == "spot":
            return []
        path = "/fapi/v3/positionRisk" if product == "usdm" else "/dapi/v1/positionRisk"
        response = self.client.signed_get(product, path)
        return response if isinstance(response, list) else []

    def wallet_history(self, kind: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        path = "/sapi/v1/capital/deposit/hisrec" if kind == "deposit" else "/sapi/v1/capital/withdraw/history"
        records: list[dict[str, Any]] = []
        for start_ms, end_ms in time_windows(start, end, timedelta(days=89)):
            offset = 0
            while True:
                page = self.client.signed_get(
                    "spot",
                    path,
                    {"startTime": start_ms, "endTime": end_ms, "offset": offset, "limit": 1000},
                )
                if not isinstance(page, list):
                    break
                records.extend(item for item in page if isinstance(item, dict))
                if len(page) < 1000:
                    break
                offset += len(page)
        return records

    def spot_trades(self, symbols: list[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for symbol in symbols:
            params: dict[str, Any] = {"symbol": symbol, "startTime": to_ms(start), "limit": 1000}
            while True:
                page = self.client.signed_get("spot", "/api/v3/myTrades", params)
                if not isinstance(page, list) or not page:
                    break
                rows = [item for item in page if isinstance(item, dict)]
                end_ms = to_ms(end)
                records.extend(item for item in rows if int(item.get("time", 0)) <= end_ms)
                if len(page) < 1000 or max(int(item.get("time", 0)) for item in rows) > end_ms:
                    break
                params = {"symbol": symbol, "fromId": max(int(item["id"]) for item in rows) + 1, "limit": 1000}
        return records

    def futures_trades(
        self,
        product: BinanceProduct,
        start: datetime,
        end: datetime,
        *,
        usdm_symbols: list[str] | None = None,
        coinm_pairs: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        path = "/fapi/v1/userTrades" if product == "usdm" else "/dapi/v1/userTrades"
        records: list[dict[str, Any]] = []
        scopes = list(usdm_symbols or []) if product == "usdm" else list(coinm_pairs or [])
        for scope in scopes:
            for start_ms, end_ms in time_windows(start, end, timedelta(days=7)):
                next_trade_id: int | None = None
                while True:
                    params: dict[str, Any] = {"startTime": start_ms, "endTime": end_ms, "limit": 1000}
                    params["symbol" if product == "usdm" else "pair"] = scope
                    if next_trade_id is not None:
                        params["fromId"] = next_trade_id
                    page = self.client.signed_get(product, path, params)
                    if not isinstance(page, list) or not page:
                        break
                    rows = [item for item in page if isinstance(item, dict)]
                    records.extend(rows)
                    if len(page) < 1000:
                        break
                    ids = [int(item["id"]) for item in rows if item.get("id") is not None]
                    if not ids:
                        break
                    candidate = max(ids) + 1
                    if next_trade_id is not None and candidate <= next_trade_id:
                        break
                    next_trade_id = candidate
        return records

    def futures_income(self, product: BinanceProduct, start: datetime, end: datetime) -> list[dict[str, Any]]:
        path = "/fapi/v1/income" if product == "usdm" else "/dapi/v1/income"
        window = timedelta(days=7) if product == "usdm" else timedelta(days=365)
        records: list[dict[str, Any]] = []
        for start_ms, end_ms in time_windows(start, end, window):
            page_number = 1
            while True:
                page = self.client.signed_get(
                    product,
                    path,
                    {"startTime": start_ms, "endTime": end_ms, "page": page_number, "limit": 1000},
                )
                if not isinstance(page, list):
                    break
                records.extend(item for item in page if isinstance(item, dict))
                if len(page) < 1000:
                    break
                page_number += 1
        return records
