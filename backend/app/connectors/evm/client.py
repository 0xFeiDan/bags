import time
from collections.abc import Callable
from typing import Any

import httpx


class EvmRpcError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str,
        code: int | None = None,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


class EvmRpcClient:
    """JSON-RPC client with a hard allowlist of read-only EVM methods."""

    ALLOWED_METHODS = {
        "eth_blockNumber",
        "eth_call",
        "eth_chainId",
        "eth_getBalance",
        "eth_getBlockByNumber",
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
    }

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url
        self._max_retries = max(1, max_retries)
        self._sleep = sleep
        self._request_id = 0
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "bags-portfolio/0.4", "Accept": "application/json"},
        )

    def __enter__(self) -> "EvmRpcClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError("unsupported or mutable EVM RPC method")
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or []}
        for attempt in range(self._max_retries):
            try:
                response = self._http.post(self._base_url, json=body)
            except httpx.RequestError as error:
                if attempt + 1 < self._max_retries:
                    self._sleep(min(2**attempt, 4))
                    continue
                raise EvmRpcError("EVM RPC network request failed", method=method) from error
            retry_after = self._retry_after(response)
            if response.status_code == 429 and attempt + 1 < self._max_retries and retry_after <= 5:
                self._sleep(retry_after)
                continue
            if response.status_code >= 500 and attempt + 1 < self._max_retries:
                self._sleep(min(2**attempt, 4))
                continue
            if response.status_code >= 400:
                raise EvmRpcError(
                    "EVM RPC HTTP request failed",
                    method=method,
                    status_code=response.status_code,
                    retry_after=retry_after if response.status_code == 429 else None,
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise EvmRpcError("EVM RPC returned non-JSON data", method=method) from error
            if not isinstance(payload, dict):
                raise EvmRpcError("EVM RPC returned an invalid response", method=method)
            rpc_error = payload.get("error")
            if isinstance(rpc_error, dict):
                message = str(rpc_error.get("message") or "EVM RPC request failed")[:300]
                code = rpc_error.get("code") if isinstance(rpc_error.get("code"), int) else None
                raise EvmRpcError(message, method=method, code=code)
            if "result" not in payload:
                raise EvmRpcError("EVM RPC response is missing result", method=method)
            return payload["result"]
        raise EvmRpcError("EVM RPC retry budget exhausted", method=method)

    def batch_call(self, method: str, params_list: list[list[Any]]) -> list[Any]:
        if method not in self.ALLOWED_METHODS:
            raise ValueError("unsupported or mutable EVM RPC method")
        if not params_list:
            return []
        if len(params_list) > 100:
            raise ValueError("EVM RPC batch cannot exceed 100 calls")
        bodies: list[dict[str, Any]] = []
        request_ids: list[int] = []
        for params in params_list:
            self._request_id += 1
            request_ids.append(self._request_id)
            bodies.append({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params})
        for attempt in range(self._max_retries):
            try:
                response = self._http.post(self._base_url, json=bodies)
            except httpx.RequestError as error:
                if attempt + 1 < self._max_retries:
                    self._sleep(min(2**attempt, 4))
                    continue
                raise EvmRpcError("EVM RPC batch network request failed", method=method) from error
            retry_after = self._retry_after(response)
            if response.status_code == 429 and attempt + 1 < self._max_retries and retry_after <= 5:
                self._sleep(retry_after)
                continue
            if response.status_code >= 500 and attempt + 1 < self._max_retries:
                self._sleep(min(2**attempt, 4))
                continue
            if response.status_code >= 400:
                raise EvmRpcError("EVM RPC batch HTTP request failed", method=method, status_code=response.status_code)
            try:
                payload = response.json()
            except ValueError as error:
                raise EvmRpcError("EVM RPC batch returned non-JSON data", method=method) from error
            if not isinstance(payload, list):
                raise EvmRpcError("EVM RPC endpoint does not support batch requests", method=method)
            indexed = {item.get("id"): item for item in payload if isinstance(item, dict)}
            results: list[Any] = []
            for request_id in request_ids:
                item = indexed.get(request_id)
                if not item:
                    raise EvmRpcError("EVM RPC batch response is incomplete", method=method)
                rpc_error = item.get("error")
                if isinstance(rpc_error, dict):
                    raise EvmRpcError(
                        str(rpc_error.get("message") or "EVM RPC batch item failed")[:300],
                        method=method,
                        code=rpc_error.get("code") if isinstance(rpc_error.get("code"), int) else None,
                    )
                if "result" not in item:
                    raise EvmRpcError("EVM RPC batch item is missing result", method=method)
                results.append(item["result"])
            return results
        raise EvmRpcError("EVM RPC batch retry budget exhausted", method=method)

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            return max(1, int(response.headers.get("Retry-After", "1")))
        except ValueError:
            return 1
