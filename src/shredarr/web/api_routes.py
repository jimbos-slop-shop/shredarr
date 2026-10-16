"""The dashboard's data and actions: torrents, plans, Delete now, Undo, Redownload, History, the activity log."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from ..app import spawn
from .deps import Core, JsonBody
from .responses import as_json, ok_or_home, text

router = APIRouter()
HASH = re.compile(r"[0-9a-f]{40}")


def hashes_of(body: JsonBody) -> list[str]:
    return list(body.get("hashes") or [body.get("hash", "")])


@router.get("/api/torrents")
def torrents(core: Core) -> Response:
    try:
        return as_json(core.dashboard.torrents())
    except Exception as e:  # shown on the dashboard, which keeps polling
        return text(f"{core.config.load().client_name}: {e}", 502)


@router.get("/api/plan")
def delete_plan(core: Core, hash: str = "") -> Response:  # noqa: A002 (the UI's parameter name)
    try:
        plan = core.delete_now.plan(core.config.load(), hash)
    except Exception as e:  # shown in the confirm dialog
        return text(f"could not build the plan: {e}", 502)
    return as_json({"name": plan.torrent["name"] if plan.torrent else hash, "steps": [s.desc for s in plan.steps],
                    "notes": [s.desc for s in plan.steps if s.note], "dry_run": core.config.load().dry_run})


@router.post("/api/delete-now")
def delete_now(core: Core, body: JsonBody) -> Response:
    hashes = hashes_of(body)
    spawn(lambda: [core.delete_now.run(h) for h in hashes])
    return text("started", 202)


@router.get("/api/orphans")
def orphans(core: Core, refresh: str = "") -> Response:
    try:
        ts, items = core.orphans.items(0 if refresh else 300)
    except Exception as e:  # shown on the tab
        return text(f"couldn't check the library: {e}", 502)
    return as_json({"items": items, "ts": ts})


@router.get("/api/orphan-plan")
def orphan_plan(core: Core, ids: str = "") -> Response:
    try:
        steps, done, _ = core.orphans.plan(core.config.load(), ids.split(","))
    except Exception as e:  # shown in the confirm dialog
        return text(f"could not build the plan: {e}", 502)
    return as_json({"steps": [s.desc for s in steps], "items": [x[0] for x in done], "size": sum(x[1] for x in done)})


@router.post("/api/orphans/delete")
def delete_orphans(core: Core, body: JsonBody) -> Response:
    spawn(core.orphans.delete, [str(x) for x in body.get("ids", [])])
    return text("started", 202)


@router.get("/api/undo-plan")
def undo_plan(core: Core, hash: str = "") -> Response:  # noqa: A002
    try:
        t, rows, problem = core.undo.info(hash)
    except Exception as e:  # shown in the confirm dialog
        return text(f"could not check: {e}", 502)
    return as_json({"name": rows[0][1] if rows else hash, "problem": problem, "count": len(rows),
                    "steps": core.undo.steps(t, rows) if t and not problem else []})


@router.post("/api/undo")
def undo(core: Core, body: JsonBody) -> Response:
    hashes = hashes_of(body)
    spawn(lambda: [core.undo.run(h) for h in hashes])
    return text("started", 202)


@router.get("/api/redownload-plan")
def redownload_plan(core: Core, id: str = "0") -> Response:  # noqa: A002
    try:
        return as_json(core.redownloader.plan(int(id)))
    except Exception as e:  # shown in the dialog
        return as_json({"problem": str(e)})


@router.post("/api/redownload")
def redownload(core: Core, body: JsonBody) -> Response:
    spawn(core.redownloader.run, int(body.get("id", 0)), "same" if body.get("how") == "same" else "search")
    return text("started", 202)


@router.get("/api/history")
def history(core: Core, q: str = "") -> Response:
    rows = core.dashboard.history_rows(q)
    return as_json({"rows": rows, "total": sum(r["size"] or 0 for r in rows)})


@router.get("/api/health")
def health(core: Core) -> Response:
    return as_json(core.health.status)


@router.get("/api/log")
def activity(core: Core) -> Response:
    return as_json(core.activity.recent(500))


@router.post("/api/queue")
def queue_leftovers(core: Core, body: JsonBody) -> Response:
    """Leftovers (torrents whose media is gone, but which were never queued) go on the cleanup list."""
    hashes = [h for h in body.get("hashes", []) if HASH.fullmatch(h)]
    ts = {t["hash"]: t for t in core.connections.torrents.list()}
    for h in hashes:
        if h in ts:
            core.queue.add([h], core.library.title_of(h) or ts[h]["name"], "leftover")
    core.activity.info(f"Added {len(hashes)} leftover torrent{'s' * (len(hashes) != 1)} to the cleanup list")
    spawn(core.cleaner.safe_sweep)
    return text("ok")


@router.post("/api/rule")
def torrent_rule(core: Core, body: JsonBody) -> Response:
    hashes = hashes_of(body)
    if len(hashes) == 1:
        name = next((t["name"] for t in core.connections.torrents.list(hashes[0])), hashes[0])
        label = f"'{name}'"
    else:
        label = f"{len(hashes)} torrents"
    core.editor.torrent_rule(hashes, body.get("mode") or "", body.get("min_seed_days"), body.get("min_ratio"), label)
    return text("ok")


@router.post("/rules")
def default_rule(core: Core, request: Request, mode: Annotated[str, Form()] = "either", min_seed_days: Annotated[str, Form()] = "",
                 min_ratio: Annotated[str, Form()] = "", dry_run: Annotated[str | None, Form()] = None) -> Response:
    form = {"mode": mode, "min_seed_days": min_seed_days, "min_ratio": min_ratio}
    if dry_run is not None:
        form["dry_run"] = dry_run
    core.editor.default_rule(form)
    return ok_or_home(request)


@router.post("/sweep")
def sweep(core: Core, request: Request) -> Response:
    core.cleaner.safe_sweep()
    return ok_or_home(request)
