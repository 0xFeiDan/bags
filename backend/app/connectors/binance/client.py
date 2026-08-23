import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

BinanceProduct = Literal["spot", "usdm", "coinm"]


class BinanceApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: int | str | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


class BinanceApiClient:
    """HMAC-only Binance reader. This class intentionally exposes GET operations only."""

    TIME_PATHS: dict[BinanceProduct, str] = {
        "spot": "/api/v3/time",
        "usdm": "/fapi/v1/time",
        "coinm": "/dapi/v1/time",
    }

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_urls: Mapping[BinanceProduct, str],
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Binance HMAC API key and secret are required")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._base_urls = {key: value.rstrip("/") for key, value in base_urls.items()}
        self._max_retries = max(1, max_retries)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep
        self._time_offsets: dict[BinanceProduct, int] = {}
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "bags-portfolio/0.2", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BinanceApiClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def public_get(self, product: BinanceProduct, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._send(product, path, params or {}, signed=False)

    def signed_get(self, product: BinanceProduct, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if product not in self._time_offsets:
            self.sync_server_time(product)
        return self._send(product, path, params or {}, signed=True)

    def sync_server_time(self, product: BinanceProduct) -> int:
        response = self.public_get(product, self.TIME_PATHS[product])
        try:
            server_time = int(response["serverTime"])
        except (KeyError, TypeError, ValueError) as error:
            raise BinanceApiError("Binance time response was invalid") from error
        offset = server_time - self._now_ms()
        self._time_offsets[product] = offset
        return offset

    def _send(self, product: BinanceProduct, path: str, params: Mapping[str, Any], *, signed: bool) -> Any:
        base_url = self._base_urls[product]
        for attempt in range(self._max_retries):
            query_params = [(key, value) for key, value in params.items() if value is not None]
            headers: dict[str, str] = {}
            if signed:
                query_params.extend(
                    [
                        ("recvWindow", 5000),
                        ("timestamp", self._now_ms() + self._time_offsets.get(product, 0)),
                    ]
                )
                payload = urlencode(query_params)
                signature = hmac.new(self._api_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
                query = f"{payload}&signature={signature}"
                headers["X-MBX-APIKEY"] = self._api_key
            else:
                query = urlencode(query_params)

            url = f"{base_url}{path}" + (f"?{query}" if query else "")
            try:
                response = self._http.get(url, headers=headers)
            except httpx.RequestError as error:
                if attempt + 1 < self._max_retries:
                    self._sleep(min(2**attempt, 4))
                    continue
                raise BinanceApiError("Binance network request failed") from error

            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as error:
                    raise BinanceApiError("Binance returned non-JSON data", status_code=response.status_code) from error

            body = self._error_body(response)
            code = body.get("code")
            message = str(body.get("msg") or "Binance API request failed")[:300]
            if signed and code == -1021 and attempt + 1 < self._max_retries:
                self.sync_server_time(product)
                continue
            if response.status_code in {418, 429}:
                retry_after = self._retry_after(response)
                if attempt + 1 < self._max_retries and retry_after <= 5:
                    self._sleep(max(1, retry_after))
                    continue
                raise BinanceApiError(message, status_code=response.status_code, code=code, retry_after=retry_after)
            if response.status_code >= 500 and attempt + 1 < self._max_retries:
                self._sleep(min(2**attempt, 4))
                continue
            raise BinanceApiError(message, status_code=response.status_code, code=code)

        raise BinanceApiError("Binance request retry budget exhausted")

    @staticmethod
    def _error_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            return max(1, int(response.headers.get("Retry-After", "1")))
        except ValueError:
            return 1
