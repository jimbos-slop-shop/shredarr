"""Request helpers shared by the routes."""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any

from fastapi import Body, Depends, Request

from ..app import Shredarr
from .responses import SESSION_COOKIE


def get_core(request: Request) -> Shredarr:
    core: Shredarr = request.app.state.core
    return core


Core = Annotated[Shredarr, Depends(get_core)]
JsonBody = Annotated[dict[str, Any], Body()]


def client_ip(request: Request) -> str:
    """Who is signing in, for the failed sign-in limit. X-Forwarded-For can be written by anyone, so it's only used
    when the connection comes from a private network address (a reverse proxy in front of Shredarr), and then only
    its last entry, which that proxy added itself."""
    peer = peer_ip(request)
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
    return forwarded if forwarded and private_address(peer) else peer


def peer_ip(request: Request) -> str:
    """The address the connection itself comes from (a proxy's, behind one)."""
    return request.client.host if request.client else ""


def private_address(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def is_local(request: Request) -> bool:
    """Scripts inside the container (health check, set-up tools) don't need to sign in. A request a reverse proxy
    passed on isn't local, even when the proxy runs on the same machine: proxies add one of these headers."""
    return (
        bool(request.client)
        and request.client.host == "127.0.0.1"  # type: ignore[union-attr]
        and not any(h in request.headers for h in PROXY_HEADERS)
    )
