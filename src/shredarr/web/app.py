"""The web app: the UI's pages and API, and the Jellyfin webhook."""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from ..app import Shredarr
from . import api_routes, auth_routes, pages, settings_routes
from .deps import is_local, session_token
from .responses import html, redirect, text

PUBLIC_GET = {"/health", "/login", "/reset", "/setup"}
PUBLIC_POST = {"/login", "/reset", "/reset/code", "/setup"}
WEBHOOK = "/webhook/jellyfin"


def create_app(core: Shredarr, *, start: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if start:
            await run_in_threadpool(core.start)
        yield

    app = FastAPI(
        title="Shredarr", version=core.version, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.core = core

    @app.middleware("http")
    async def sign_in(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Everything but the sign-in pages, the logo, the health check and the webhook needs a session."""
        path, method = request.url.path, request.method
        if method == "GET" and pages.static_name(core.settings.static_dir, path):
            return await call_next(request)
        request.state.authed = is_local(request) or (
            core.auth.has_password and await run_in_threadpool(core.auth.valid, session_token(request))
        )
        public = (method == "GET" and path in PUBLIC_GET) or (
            method == "POST" and (path in PUBLIC_POST or path.startswith(WEBHOOK))
        )
        if public or request.state.authed:
            return await call_next(request)
        if path.startswith("/api/") or request.headers.get("X-Fetch") or method == "POST":
            return text("sign in required", 401)
        return redirect("/login" if core.auth.has_password else "/setup")

    @app.exception_handler(HTTPException)
    async def not_found(request: Request, exc: HTTPException) -> Response:
        return text("not found", 404) if exc.status_code in (404, 405) else text(str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        return text("bad request", 400)

    for router in (auth_routes.router, api_routes.router, settings_routes.router):
        app.include_router(router)

    @app.get("/health")
    def health() -> Response:
        return text("ok")

    @app.get("/")
    @app.get("/settings")
    def page() -> Response:
        return html(pages.main_page(core))

    @app.post(WEBHOOK + "{rest:path}")
    async def jellyfin_webhook(request: Request) -> Response:
        """Jellyfin's Webhook plugin authenticates with the shared token, not a session."""
        token = core.config.load().get("webhook_token")
        if token and not hmac.compare_digest(request.headers.get("X-Webhook-Token", ""), token):
            return text("bad token", 401)
        try:
            core.events.put(json.loads(await request.body()))
        except ValueError:
            return text("bad json", 400)
        return text("queued")

    @app.get("/{path:path}")  # registered last: everything else is matched first
    def static(request: Request) -> Response:
        name = pages.static_name(core.settings.static_dir, request.url.path)
        if not name:
            return text("not found", 404)
        content, ctype = pages.static_file(core.settings.static_dir, name)
        return Response(content, headers={"Content-Type": ctype, "Cache-Control": "max-age=86400"})

    return app
