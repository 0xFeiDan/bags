from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.zerion.limits import ZerionRequestGovernor, extract_rate_limits


class ZerionApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, rate_limits: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.rate_limits = rate_limits or {}


class ZerionRateLimitError(ZerionApiError):
    pass


@dataclass(frozen=True)
class ZerionPage:
    data: list[dict[str, Any]]
    next_url: str | None
    rate_limits: dict[str, Any]


class ZerionApiClient:
    """GET-only Zerion client. It exposes no swap, bridge, signing, or wallet methods."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        governor: ZerionRequestGovernor,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Zerion API key is required")
        self._base_url = base_url.rstrip("/")
        self._governor = governor
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            auth=(api_key, ""),
            headers={"Accept": "application/json", "User-Agent": "bags-portfolio/0.9"},
        )

    @property
    def remaining_request_budget(self) -> int:
        return self._governor.remaining_run_budget

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ZerionApiClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def wallet_transactions(self, address: str, chain_id: str, *, url: str | None = None) -> ZerionPage:
        return self._get_page(
            url or f"{self._base_url}/v1/wallets/{address}/transactions/",
            None if url else {"currency": "usd", "filter[chain_ids]": chain_id, "page[size]": 100},
        )

    def wallet_simple_positions(self, address: str, chain_id: str) -> ZerionPage:
        return self._get_page(
            f"{self._base_url}/v1/wallets/{address}/positions/",
            {
                "currency": "usd",
                "filter[chain_ids]": chain_id,
                "filter[positions]": "only_simple",
                "page[size]": 100,
            },
        )

    def _get_page(self, url: str, params: dict[str, Any] | None) -> ZerionPage:
        self._validate_url(url)
        self._governor.reserve()
        try:
            response = self._http.get(url, params=params)
        except httpx.RequestError as error:
            raise ZerionApiError("Zerion network request failed") from error

        rate_limits = extract_rate_limits(response.headers)
        self._governor.record_rate_limits(rate_limits)
        if response.status_code == 429:
            raise ZerionRateLimitError(
                "Zerion request was rate limited; automatic retry is disabled",
                status_code=429,
                rate_limits=rate_limits,
            )
        if response.status_code >= 400:
            raise ZerionApiError(
                self._error_message(response),
                status_code=response.status_code,
                rate_limits=rate_limits,
            )
        try:
            body = response.json()
        except ValueError as error:
            raise ZerionApiError("Zerion returned non-JSON data", status_code=response.status_code) from error
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ZerionApiError("Zerion page response is missing a data list", status_code=response.status_code)
        links = body.get("links") if isinstance(body.get("links"), dict) else {}
        next_url = links.get("next")
        if next_url is not None:
            if not isinstance(next_url, str):
                raise ZerionApiError("Zerion next-page URL is invalid", status_code=response.status_code)
            self._validate_url(next_url)
        data = [item for item in body["data"] if isinstance(item, dict)]
        return ZerionPage(data=data, next_url=next_url, rate_limits=rate_limits)

    def _validate_url(self, url: str) -> None:
        base = urlparse(self._base_url)
        candidate = urlparse(url)
        if candidate.scheme != base.scheme or candidate.netloc != base.netloc or not candidate.path.startswith("/v1/"):
            raise ZerionApiError("Zerion pagination URL left the configured API origin")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return "Zerion API request failed"
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("detail") or errors[0].get("title") or "Zerion API request failed")[:300]
        return "Zerion API request failed"
