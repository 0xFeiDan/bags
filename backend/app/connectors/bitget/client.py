import base64
import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx


class BitgetApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: int | str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class BitgetApiClient:
    """Bitget HMAC reader. Only signed GET operations are exposed."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base_url: str = "https://api.bitget.com",
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not api_secret or not passphrase:
            raise ValueError("Bitget API key, secret, and passphrase are required")
        self._api_key = api_key
        self._secret = api_secret.encode()
        self._passphrase = passphrase
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(1, max_retries)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout_seconds, transport=transport, headers={"Accept": "application/json", "User-Agent": "bags-portfolio/0.8"})

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def signed_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        query = urlencode(sorted((key, value) for key, value in (params or {}).items() if value is not None))
        suffix = f"?{query}" if query else ""
        for attempt in range(self._max_retries):
            timestamp = str(self._now_ms())
            prehash = f"{timestamp}GET{path}{suffix}"
            signature = base64.b64encode(hmac.new(self._secret, prehash.encode(), hashlib.sha256).digest()).decode()
            headers = {
                "ACCESS-KEY": self._api_key,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self._passphrase,
                "locale": "en-US",
            }
            try:
                response = self._http.get(f"{self._base_url}{path}{suffix}", headers=headers)
            except httpx.RequestError as error:
                if attempt + 1 < self._max_retries:
                    self._sleep(min(2**attempt, 4))
                    continue
                raise BitgetApiError("Bitget network request failed") from error
            try:
                body = response.json()
            except ValueError as error:
                raise BitgetApiError("Bitget returned non-JSON data", status_code=response.status_code) from error
            code = body.get("code") if isinstance(body, dict) else None
            if response.status_code < 400 and code in {"00000", 0, "0"}:
                return body.get("data")
            message = str(body.get("msg") if isinstance(body, dict) else "Bitget API request failed")[:300]
            if (response.status_code == 429 or response.status_code >= 500) and attempt + 1 < self._max_retries:
                self._sleep(min(2**attempt, 4))
                continue
            raise BitgetApiError(message, status_code=response.status_code, code=code)
        raise BitgetApiError("Bitget request retry budget exhausted")
