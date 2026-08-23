import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx


class BybitApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: int | str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class BybitApiClient:
    """Bybit V5 HMAC reader. Only GET is intentionally implemented."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.bybit.com",
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Bybit HMAC API key and secret are required")
        self._api_key = api_key
        self._secret = api_secret.encode()
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(1, max_retries)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout_seconds, transport=transport, headers={"Accept": "application/json", "User-Agent": "bags-portfolio/0.8"})

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def public_get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._send(path, params or {}, signed=False)

    def signed_get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._send(path, params or {}, signed=True)

    def _send(self, path: str, params: Mapping[str, Any], *, signed: bool) -> dict[str, Any]:
        query = urlencode([(key, value) for key, value in params.items() if value is not None])
        for attempt in range(self._max_retries):
            headers: dict[str, str] = {}
            if signed:
                timestamp = str(self._now_ms())
                recv_window = "5000"
                signature = hmac.new(self._secret, f"{timestamp}{self._api_key}{recv_window}{query}".encode(), hashlib.sha256).hexdigest()
                headers = {
                    "X-BAPI-API-KEY": self._api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": recv_window,
                    "X-BAPI-SIGN": signature,
                }
            try:
                response = self._http.get(f"{self._base_url}{path}" + (f"?{query}" if query else ""), headers=headers)
            except httpx.RequestError as error:
                if attempt + 1 < self._max_retries:
                    self._sleep(min(2**attempt, 4))
                    continue
                raise BybitApiError("Bybit network request failed") from error
            try:
                body = response.json()
            except ValueError as error:
                raise BybitApiError("Bybit returned non-JSON data", status_code=response.status_code) from error
            code = body.get("retCode") if isinstance(body, dict) else None
            if response.status_code < 400 and code in {0, "0"}:
                return body
            message = str(body.get("retMsg") if isinstance(body, dict) else "Bybit API request failed")[:300]
            if (response.status_code in {429} or response.status_code >= 500 or code in {10006, "10006"}) and attempt + 1 < self._max_retries:
                self._sleep(min(2**attempt, 4))
                continue
            raise BybitApiError(message, status_code=response.status_code, code=code)
        raise BybitApiError("Bybit request retry budget exhausted")
