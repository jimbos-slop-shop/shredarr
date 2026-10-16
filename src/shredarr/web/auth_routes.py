"""First-time setup, sign-in, password reset and sign-out."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from ..services.auth import SESSION_DAYS
from . import pages
from .deps import Core, JsonBody, client_ip, peer_ip, session_token
from .responses import html, redirect, session_cookie, text

router = APIRouter()
SESSION_AGE = SESSION_DAYS * 86400
FormField = Annotated[str, Form()]


@router.get("/setup")
def setup_form(core: Core) -> Response:
    return redirect("/") if core.auth.has_password else html(pages.setup_page(core))


@router.post("/setup")
def setup(core: Core, request: Request, new: FormField = "", confirm: FormField = "") -> Response:
    if core.auth.has_password:  # only before a password exists
        return text("already set up", 403)
    if new != confirm:
        return html(pages.setup_page(core, "The passwords don't match."), 400)
    if errs := core.auth.policy_errors(new):
        return html(pages.setup_page(core, "The password needs " + ", ".join(errs) + "."), 400)
    core.auth.set_password(new)
    core.activity.info("Password created during first-time setup")
    return redirect("/settings", [session_cookie(request, core.auth.new_session(), SESSION_AGE)])


@router.get("/login")
def login_form(core: Core, request: Request) -> Response:
    if not core.auth.has_password:
        return redirect("/setup")
    return redirect("/") if request.state.authed else html(pages.login_page(core))


@router.post("/login")
def login(core: Core, request: Request, password: FormField = "") -> Response:
    ip, peer = client_ip(request), peer_ip(request)
    if core.auth.login_blocked(ip, peer):
        return html(pages.login_page(core, "Too many attempts. Try again in 15 minutes."), 429)
    if not core.auth.check(password):
        core.auth.login_failed(ip, peer)
        time.sleep(1)
        return html(pages.login_page(core, "That password isn't right."), 401)
    core.auth.login_ok(ip)
    return redirect("/", [session_cookie(request, core.auth.new_session(), SESSION_AGE)])


@router.get("/reset")
def reset_form(core: Core) -> Response:
    return html(pages.reset_page(core)) if core.auth.has_password else redirect("/setup")


@router.post("/reset/code")
def reset_code(core: Core) -> Response:
    if core.auth.make_reset_code():
        return html(pages.reset_page(core, "Code sent. Check your notifications, or the file on the server.", ok=True))
    return html(pages.reset_page(core, "A code was just made. Wait a minute before asking for another."), 429)


@router.post("/reset")
def reset(core: Core, request: Request, code: FormField = "", new: FormField = "", confirm: FormField = "") -> Response:
    if new != confirm:
        return html(pages.reset_page(core, "The new passwords don't match."), 400)
    if errs := core.auth.policy_errors(new):
        return html(pages.reset_page(core, "The new password needs " + ", ".join(errs) + "."), 400)
    if not core.auth.use_reset_code(code):
        time.sleep(1)
        return html(pages.reset_page(core, "That code isn't valid or has expired. Ask for a new one."), 400)
    core.auth.set_password(new)
    core.sessions.remove_all()
    core.activity.info("Password reset with a reset code (all devices were signed out)")
    return redirect("/", [session_cookie(request, core.auth.new_session(), SESSION_AGE)])


@router.post("/logout")
def logout(core: Core, request: Request) -> Response:
    core.sessions.remove(session_token(request))
    return redirect("/login", [session_cookie(request, "", 0)])


@router.post("/api/logout-all")
def logout_all(core: Core, request: Request) -> Response:
    core.sessions.remove_all()
    core.activity.info("Signed out everywhere")
    return text("ok", headers=[session_cookie(request, "", 0)])


@router.post("/api/password")
def change_password(core: Core, request: Request, body: JsonBody) -> Response:
    if not core.auth.check(body.get("current", "")):
        return text("Your current password isn't right.", 400)
    if errs := core.auth.policy_errors(body.get("new", "")):
        return text("The new password needs " + ", ".join(errs) + ".", 400)
    core.auth.set_password(body["new"])
    core.sessions.remove_all(keep=session_token(request))
    core.activity.info("Password changed (other devices were signed out)")
    return text("ok")
