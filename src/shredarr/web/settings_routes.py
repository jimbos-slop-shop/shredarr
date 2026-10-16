"""The Settings page: its data, saving each section, and connection tests."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..app import spawn
from ..clients.base import ReadOnly, ServiceHTTPError
from ..config import SECRET_KEYS
from ..services.connections import describe_service
from ..services.settings_editor import Invalid
from ..text import plural
from .deps import Core, JsonBody
from .responses import as_json, text

router = APIRouter()


@router.get("/api/settings")
def settings(core: Core, request: Request) -> Response:
    host = (request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "").split(":")[0]
    return as_json(core.dashboard.settings(host))


@router.post("/api/settings")
def save_notifications(core: Core, body: JsonBody) -> Response:
    try:
        core.editor.notifications(body.get("notify", {}))
    except Invalid as e:
        return text(str(e), 400)
    core.discord_logo()
    return text("ok")


@router.post("/api/notify-test")
def notify_test(core: Core) -> Response:
    core.discord_logo()
    try:
        tried, errs = core.notifier.send(core.config.load().notify, "Shredarr test", "Notifications are working. 🗑️")
    except ReadOnly as e:
        return text(str(e).capitalize() + ".", 400)
    if not tried:
        return text("Add at least one service first, and save.", 400)
    return text("; ".join(errs) or "Sent to " + ", ".join(tried), 502 if errs else 200)


@router.post("/api/connections")
def save_connections(core: Core, body: JsonBody) -> Response:
    core.editor.connections(body)
    core.settings_changed()
    spawn(core.health.check)
    return text("ok")


@router.post("/api/test-connection")
def test_connection(core: Core, body: JsonBody) -> Response:
    svc, conf = body.get("service"), dict(body.get("conf") or {})
    c = core.config.load()
    saved = c.service(svc or "") or next((x for x in c.get("extra_arrs", []) if x.get("key") == svc), {})
    for k in SECRET_KEYS:  # a blank key or password means the saved one
        if not conf.get(k) and saved.get(k):
            conf[k] = saved[k]
    try:
        return as_json({"ok": True, "msg": describe_service(svc or "", conf)})
    except ServiceHTTPError as e:
        msg = f"HTTP {e.status}" + (": check the API key" if e.auth_problem else "")
    except Exception as e:  # any failure is the answer to "does it work?"
        msg = str(e)
    return as_json({"ok": False, "msg": msg})


@router.post("/api/jellyfin-webhook")
def jellyfin_webhook(core: Core, body: JsonBody) -> Response:
    try:
        return text("Jellyfin will now send deletes to " + core.webhook.setup(body.get("url", "")))
    except Exception as e:  # shown next to the button
        return text(f"Couldn't set up the webhook: {e}", 400)


@router.post("/api/behavior")
def save_behavior(core: Core, body: JsonBody) -> Response:
    core.editor.behavior(body)
    return text("ok")


@router.post("/api/rule-list")
def save_rule_list(core: Core, body: JsonBody) -> Response:
    core.editor.rule_list(body.get("rules", []))
    return text("ok")


@router.post("/api/low-space")
def save_low_space(core: Core, body: JsonBody) -> Response:
    try:
        core.editor.low_space(body)
    except Invalid as e:
        return text(str(e), 400)
    return text("ok")


@router.post("/api/path-maps")
def save_path_maps(core: Core, body: JsonBody) -> Response:
    core.editor.path_maps(body.get("maps", []))
    core.library.forget(index=False, groups=False, titles=False)
    return text("ok")


@router.post("/api/update-check")
def save_update_check(core: Core, body: JsonBody) -> Response:
    core.editor.update_check(bool(body.get("enabled")))
    spawn(core.updates.check)
    return text("ok")


@router.post("/api/grace")
def save_grace(core: Core, body: JsonBody) -> Response:
    try:
        core.editor.grace(body)
    except Invalid as e:
        return text(str(e), 400)
    return text("ok")


@router.post("/api/restore")
def save_restore(core: Core, body: JsonBody) -> Response:
    core.editor.restore(bool(body.get("enabled")))
    return text("ok")


@router.post("/api/save-torrents")
def save_torrents(core: Core, body: JsonBody) -> Response:
    core.editor.save_torrents(bool(body.get("enabled")))
    return text("ok")


@router.post("/api/torrent-retention")
def save_torrent_retention(core: Core, body: JsonBody) -> Response:
    try:
        core.editor.torrent_retention(body)
    except Invalid as e:
        return text(str(e), 400)
    core.saved.prune()  # a shorter time applies right away
    return text("ok")


@router.post("/api/saved-torrents/delete")
def delete_saved_torrents(core: Core) -> Response:
    return text(f"Deleted {plural(core.saved.delete_all(), 'saved .torrent file')}")


@router.post("/api/restore-check")
def restore_check(core: Core) -> Response:
    ok, msgs = core.restorer.check_setup()
    return as_json({"ok": ok, "messages": msgs}, 200 if ok else 400)


@router.post("/api/password-policy")
def save_password_policy(core: Core, body: JsonBody) -> Response:
    try:
        core.editor.password_policy(body)
    except Invalid as e:
        return text(str(e), 400)
    return text("ok")
