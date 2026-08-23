import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx


class HyperliquidApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class HyperliquidClient:
    """Public read-only client restricted to Hyperliquid's `/info` endpoint."""

    ALLOWED_INFO_TYPES = {
        "clearinghouseState",
        "spotClearinghouseState",
        "metaAndAssetCtxs",
        "spotMeta",
        "userFillsByTime",
        "userFunding",
        "userNonFundingLedgerUpdates",
    }

    def __init__(
        self,
        *,
        base_url: str = "https://api.hyperliquid.xyz",
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(1, max_retries)
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "bags-portfolio/0.3", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HyperliquidClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def info(self, payload: Mapping[str, Any]) -> Any:
        request_type = payload.get("type")
        if request_type not in self.ALLOWED_INFO_TYPES:
            raise ValueError("unsupported Hyperliquid read operation")
        body = {key: value for key, value in payload.items() if value is not None}
        for attempt in range(self._max_retries):
            try:
                response = self._http.post(f"{self._base_url}/info", json=body)
            except httpx.RequestError as error:
                if attempt + 1 < self._max_retries:
                    self._sleep(min(2**attempt, 4))
                    continue
                raise HyperliquidApiError("Hyperliquid network request failed") from error

            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as error:
                    raise HyperliquidApiError("Hyperliquid returned non-JSON data", status_code=response.status_code) from error

            retry_after = self._retry_after(response)
            if response.status_code == 429 and attempt + 1 < self._max_retries and retry_after <= 5:
                self._sleep(max(1, retry_after))
                continue
            if response.status_code >= 500 and attempt + 1 < self._max_retries:
                self._sleep(min(2**attempt, 4))
                continue
            raise HyperliquidApiError(
                self._error_message(response),
                status_code=response.status_code,
                retry_after=retry_after if response.status_code == 429 else None,
            )
        raise HyperliquidApiError("Hyperliquid request retry budget exhausted")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return "Hyperliquid API request failed"
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message")
            if message:
                return str(message)[:300]
        if isinstance(payload, str):
            return payload[:300]
        return "Hyperliquid API request failed"

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            return max(1, int(response.headers.get("Retry-After", "1")))
        except ValueError:
            return 1
