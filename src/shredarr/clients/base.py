"""What all clients share: one error type for the rest of Shredarr, and a small JSON-over-HTTP client."""

from __future__ import annotations

from typing import Any

import httpx

TIMEOUT = 60


class ServiceError(Exception):
    """A request to another app failed. str() is short and readable, for the activity log and Settings."""


class ServiceHTTPError(ServiceError):
    """The app answered with an HTTP error."""

    def __init__(self, status: int, reason: str = "", body: str = "") -> None:
        self.status, self.reason, self.body = status, reason, body
        super().__init__(f"HTTP Error {status}: {reason}".rstrip(": "))

    @property
    def auth_problem(self) -> bool:
        return self.status in (401, 403)


class ServiceUnreachable(ServiceError):
    """No answer at all: wrong address, the app is down, a timeout."""


class NotConfigured(ServiceError):
    """The app isn't set up in Settings → Connections."""


def describe(e: Exception) -> str:
    """How a connection problem is shown in Settings and the activity log."""
    if isinstance(e, ServiceHTTPError):
        return f"HTTP {e.status}" + (" (check the API key)" if e.auth_problem else "")
    return str(e)


def parse_body(r: httpx.Response) -> Any:
    """JSON if the app sent JSON, otherwise the text (some endpoints answer with plain text or nothing)."""
    return r.json() if r.content[:1] in (b"[", b"{") else r.text


class JsonApi:
    """A JSON API at base_url. Every request raises ServiceError on failure."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(headers=headers or {}, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            r = self._http.request(method, self.base_url + path, params=params, json=json, headers=headers)
        except httpx.HTTPError as e:
            raise ServiceUnreachable(str(e) or type(e).__name__) from e
        if r.status_code >= 400:
            raise ServiceHTTPError(r.status_code, r.reason_phrase, r.text[:500])
        return parse_body(r)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, json=body, params=params or None)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, json=body)

    def delete(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("DELETE", path, json=body, params=params or None)

    def close(self) -> None:
        self._http.close()
