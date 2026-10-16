"""Request helpers shared by the routes."""

from __future__ import annotations

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
    peer = request.client.host if request.client else ""
    return (request.headers.get("X-Forwarded-For") or peer).split(",")[0].strip()


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def is_local(request: Request) -> bool:
    """Scripts inside the container (health check, set-up tools) don't need to sign in."""
    return bool(request.client) and request.client.host == "127.0.0.1"  # type: ignore[union-attr]
