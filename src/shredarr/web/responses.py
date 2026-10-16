"""Responses the way the web UI expects them: never cached, plain text for messages it shows as they are."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from fastapi.responses import Response

SESSION_COOKIE = "shredarr_session"
NO_STORE = {"Cache-Control": "no-store"}

Header = tuple[str, str]


def reply(code: int, body: str, ctype: str = "text/plain", headers: Iterable[Header] = ()) -> Response:
    r = Response(body.encode(), code, headers={**NO_STORE, "Content-Type": ctype + "; charset=utf-8"})
    for k, v in headers:
        r.headers.append(k, v)
    return r


def text(body: str, code: int = 200, headers: Iterable[Header] = ()) -> Response:
    return reply(code, body, "text/plain", headers)


def html(body: str, code: int = 200) -> Response:
    return reply(code, body, "text/html")


def as_json(data: Any, code: int = 200) -> Response:
    return reply(code, json.dumps(data), "application/json")


def redirect(where: str, headers: Iterable[Header] = ()) -> Response:
    r = Response(status_code=303, headers={"Location": where})
    for k, v in headers:
        r.headers.append(k, v)
    return r


def session_cookie(request: Request, token: str, max_age: int) -> Header:
    secure = "; Secure" if request.headers.get("X-Forwarded-Proto") == "https" else ""
    return "Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"


def ok_or_home(request: Request) -> Response:
    """Forms posted by the page's own scripts get "ok"; a plain form post goes back to the dashboard."""
    return text("ok") if request.headers.get("X-Fetch") else redirect("/")
