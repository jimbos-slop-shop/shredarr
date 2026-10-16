"""What the web UI shows: the torrent list, History, and the Settings page's data."""

from __future__ import annotations

import contextlib
import os
import shutil

from ..clients.base import ServiceError
from ..clients.torrents import Torrent
from ..config import JSON, MODES, SECRET_KEYS, SERVICES, ConfigStore, base_url
from ..files import restorable
from ..repositories import HistoryRepo, OverridesRepo, PendingRepo
from .cleanup import Cleaner, free_gb
from .connections import Connections
from .health import Health, Updates
from .library import Library
from .rules import evaluate, met_parts
from .trackers import Trackers, tracker_host
from .webhook_setup import WebhookSetup


def hide_secrets(svc: str, conf: JSON | None) -> JSON:
    """A connection's settings for the browser: API keys and passwords blanked (has_secret says one is saved)."""
    conf = conf or {}
    return {k: ("" if k in SECRET_KEYS else (base_url(svc, v) if k == "url" else v)) for k, v in conf.items()} | {
        "has_secret": any(conf.get(k) for k in SECRET_KEYS)
    }


class Dashboard:
    def __init__(
        self,
        config: ConfigStore,
        connections: Connections,
        library: Library,
        trackers: Trackers,
        cleaner: Cleaner,
        pending: PendingRepo,
        overrides: OverridesRepo,
        history: HistoryRepo,
        health: Health,
        updates: Updates,
        webhook: WebhookSetup,
        version: str,
        repo: str,
        torrent_dir: str,
    ) -> None:
        self.config = config
        self.connections = connections
        self.library = library
        self.trackers = trackers
        self.cleaner = cleaner
        self.pending = pending
        self.overrides = overrides
        self.history = history
        self.health = health
        self.updates = updates
        self.webhook = webhook
        self.version = version
        self.repo = repo
        self.torrent_dir = torrent_dir

    def torrents(self) -> JSON:
        c = self.config.load()
        common = {
            "default": c.default_rule,
            "dry_run": c.dry_run,
            "modes": MODES,
            "health": self.health.status,
            "version": self.version,
            "update": self.updates.info,
        }
        if c.missing:
            return {"setup": c.missing, "torrents": [], **common}
        ov = self.overrides.all()
        pend = {p.hash: p for p in self.pending.all()}
        total, count, last30 = self.history.totals()
        imap = self.library.imports()
        grace = c.grace_seconds
        raw = self.connections.torrents.all()
        by_hash = {t["hash"]: t for t in raw}
        groups = self.library.cross_seed_groups(raw)
        names = self.trackers.names(raw)
        together = c.behavior["cross_seed_with_original"]
        out = []
        for t in raw:
            h = t["hash"]
            p = pend.get(h)
            orig = self.library.cross_seed_of(h, by_hash, groups)
            title = self.library.title_of(h, p.name if p else None) or (self.library.title_of(orig) if orig else None)
            r = self.cleaner.rule(t, ov, c, title)
            progress, met = evaluate(r, t)
            lib = self.library.in_library(t, max_age=60)
            managed = h in imap or any(x in imap for x in groups.get(h, ()))
            out.append(
                {
                    "hash": h,
                    "name": t["name"],
                    "title": title,
                    "category": t["category"],
                    "tracker": tracker_host(t),
                    "tracker_name": names.get(h),
                    "cross_seed_of": by_hash[orig]["name"] if orig else None,
                    "cross_seeds": len(groups.get(h, ())) - 1 if h in groups and not orig else 0,
                    "with_original": bool(orig and together and orig in pend),
                    "state": t["state"],
                    "progress": t["progress"],
                    "ratio": t["ratio"],
                    "seeding_days": t["seeding_time"] / 86400,
                    "upspeed": t["upspeed"],
                    "dlspeed": t["dlspeed"],
                    "size": t["size"],
                    "added_on": t["added_on"],
                    "rule": {k: r.get(k) for k in ("mode", "min_seed_days", "min_ratio")},
                    "rule_source": r["source"],
                    "custom": h in ov,
                    "rule_progress": progress,
                    "rule_met": met,
                    "tracked": managed,
                    **met_parts(r, t),
                    "in_library": lib,
                    "leftover": managed and not lib and h not in pend and t["progress"] >= 1,
                    "queued": h in pend,
                    "queued_as": p.name if p else None,
                    "status": p.status if p else None,
                    "delete_after": p.added_at + grace if p and grace else None,
                    "undo": None if not p else ("ok" if p.undo and restorable(t["content_path"]) else "no"),
                }
            )
        return {
            **common,
            "torrents": out,
            "freed": {"total": total, "count": count, "last30": last30},
            "free_gb": free_gb(by_hash),
        }

    def history_rows(self, q: str = "") -> list[JSON]:
        q = q.casefold()
        out = []
        for r in self.history.recent():
            if q and q not in (r.name or "").casefold() and q not in (r.title or "").casefold():
                continue
            out.append(
                {
                    "id": r.id,
                    "ts": r.ts,
                    "name": r.name,
                    "size": r.size,
                    "title": r.title,
                    "category": r.category,
                    "reason": r.reason,
                    "redownloaded": r.redownloaded,
                    "redownload_how": r.redownload_how,
                    "cross_seed_of": (r.restore or {}).get("cross_seed_of"),
                    "saved_torrent": bool(r.hash)
                    and os.path.isfile(os.path.join(self.torrent_dir, (r.hash or "") + ".torrent")),
                }
            )
        return out

    def settings(self, host: str) -> JSON:
        """Everything the Settings page shows. host: how the browser reached Shredarr (to suggest a webhook address)."""
        c = self.config.load()
        conns: JSON = {svc: hide_secrets(svc, c.get(svc)) for svc in SERVICES}
        conns["extra_arrs"] = [hide_secrets(x["key"], x) for x in c.get("extra_arrs", [])]
        conns["torrent_client"], conns["media_server"] = c.client_kind, c.media_kind
        ts: list[Torrent] = []
        if not c.missing:
            with contextlib.suppress(ServiceError):
                ts = self.connections.torrents.all()
        guess = f"http://{host or 'shredarr'}:8089"
        if c.media_kind == "jellyfin":
            with contextlib.suppress(ServiceError, KeyError):
                guess = self.webhook.current_url() or guess  # prefer the address Jellyfin's webhook already uses
        series, movies = self.library.titles() if ts else ({}, {})
        g = c.get("grace", {})
        return {
            "connections": conns,
            "health": self.health.status,
            "missing": c.missing,
            "behavior": c.behavior,
            "rule_list": c.rule_list,
            "low_space": c.low_space,
            "path_maps": c.path_maps,
            "update_check": c.get("update_check", True),
            "version": self.version,
            "update": self.updates.info,
            "repo": self.repo,
            "webhook_url_guess": guess,
            "choices": {
                "categories": sorted({t["category"] for t in ts if t["category"]}),
                "trackers": self.trackers.choices(ts),
                "titles": sorted(set(series.values()) | set(movies.values()), key=str.casefold) if ts else [],
            },
            "free_gb": free_gb({t["hash"]: t for t in ts}),
            "notify": c.notify,
            "policy": c.password_policy,
            "restore": {"enabled": c.restore_enabled},
            "grace": {"value": g.get("value") or 0, "unit": g.get("unit") or "hours"},
            "unar": bool(shutil.which("unar")),
        }
