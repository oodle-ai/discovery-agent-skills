"""HTTP client wrapper shared by collectors.

Behaviors all collectors get for free:
- fixed inter-request delay (be gentle on customer systems)
- bounded retries with backoff on connect errors and 5xx
- dead-endpoint circuit breaker: after N consecutive connection failures,
  remaining requests are skipped instead of hanging the whole run
- structured results: collectors record gaps from FetchResult instead of
  crashing or silently continuing
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

REQUEST_DELAY_S = 0.15
MAX_RETRIES = 2
RETRY_BACKOFF_S = 1.0
DEAD_AFTER_CONSECUTIVE_FAILURES = 3


@dataclass
class FetchResult:
    ok: bool
    data: Any = None
    status_code: int | None = None
    error: str | None = None
    # gap reason aligned with schemas/summary.schema.json:
    # permission_denied | auth_failed | endpoint_404 | timeout | api_error
    gap_reason: str | None = None

    @property
    def failed(self) -> bool:
        return not self.ok


class EndpointDown(Exception):
    """Raised when the circuit breaker has opened for this endpoint."""


def gap_reason_for_status(status: int) -> str:
    if status in (401,):
        return "auth_failed"
    if status in (403,):
        return "permission_denied"
    if status in (404, 405, 410):
        return "endpoint_404"
    return "api_error"


class HttpClient:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        timeout_s: float = 60.0,
        verify: bool = True,
        delay_s: float = REQUEST_DELAY_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if transport and auth:
            raise ValueError("Provide either transport or auth, not both")
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "headers": headers or {},
            "verify": verify,
            "timeout": timeout_s,
        }
        if transport:
            kwargs["transport"] = transport
        elif auth:
            kwargs["auth"] = auth
        self._client = httpx.Client(**kwargs)
        self.base_url = base_url
        self.delay_s = delay_s
        self._consecutive_conn_failures = 0
        self._dead = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def dead(self) -> bool:
        return self._dead

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        as_text: bool = False,
    ) -> FetchResult:
        """Shared request logic for GET/POST. Never raises for HTTP/connection errors."""
        if self._dead:
            return FetchResult(
                ok=False,
                error=f"endpoint marked dead after "
                f"{DEAD_AFTER_CONSECUTIVE_FAILURES} consecutive connection failures",
                gap_reason="timeout",
            )
        time.sleep(self.delay_s)
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self._client.request(method, path, params=params, json=json_body)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                    continue
                self._consecutive_conn_failures += 1
                if self._consecutive_conn_failures >= DEAD_AFTER_CONSECUTIVE_FAILURES:
                    self._dead = True
                return FetchResult(ok=False, error=str(exc), gap_reason="timeout")
            except httpx.HTTPError as exc:
                return FetchResult(ok=False, error=str(exc), gap_reason="api_error")

            self._consecutive_conn_failures = 0
            if r.status_code >= 400:
                if r.status_code >= 500 and attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                    continue
                return FetchResult(
                    ok=False,
                    status_code=r.status_code,
                    error=r.text[:300],
                    gap_reason=gap_reason_for_status(r.status_code),
                )
            if as_text:
                return FetchResult(ok=True, data=r.text, status_code=r.status_code)
            try:
                return FetchResult(ok=True, data=r.json(), status_code=r.status_code)
            except ValueError as exc:
                return FetchResult(
                    ok=False,
                    status_code=r.status_code,
                    error=f"non-JSON response: {exc}",
                    gap_reason="api_error",
                )
        # unreachable in practice; keep type-checkers happy
        return FetchResult(ok=False, error=str(last_exc), gap_reason="api_error")

    def get_text(self, path: str, params: dict[str, str] | None = None) -> FetchResult:
        """GET path, return body as plain text in data. Never raises."""
        return self._request("GET", path, params=params, as_text=True)

    def get_json(self, path: str, params: dict[str, str] | None = None) -> FetchResult:
        """GET path, expect JSON. Never raises for HTTP/connection errors."""
        return self._request("GET", path, params=params)

    def post_json(
        self,
        path: str,
        json_body: Any = None,
        params: dict[str, str] | None = None,
    ) -> FetchResult:
        """POST path with optional JSON body, expect JSON. Never raises."""
        return self._request("POST", path, params=params, json_body=json_body)
