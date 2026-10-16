"""Undo: while a deleted item's torrent still seeds, put it back in Sonarr/Radarr and the library."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from functools import partial

from ..clients.base import ServiceError
from ..clients.servarr import RadarrClient, SonarrClient
from ..clients.torrents import Torrent
from ..config import JSON, ConfigStore
from ..repositories import PendingRepo
from .activity import Activity
from .connections import Connections
from .library import Library
from .media_files import restorable
from .notifications import Notifier
from .restore import Restorer


def snap_app(snap: JSON) -> str:
    """The Sonarr/Radarr instance an Undo snapshot belongs to."""
    return str(snap.get("app") or ("radarr" if snap["kind"] == "movie" else "sonarr"))


UndoRow = tuple[str, str, JSON | None]  # (hash, what was deleted, Undo snapshot)


class Undo:
    """Bring back media whose torrent is still waiting for cleanup."""

    def __init__(
        self,
        config: ConfigStore,
        connections: Connections,
        library: Library,
        restorer: Restorer,
        pending: PendingRepo,
        activity: Activity,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.connections = connections
        self.library = library
        self.restorer = restorer
        self.pending = pending
        self.activity = activity
        self.notifier = notifier

    def info(self, h: str) -> tuple[Torrent | None, list[UndoRow], str | None]:
        """(torrent, rows, problem). A whole-series delete is undone as one, across all its torrents."""
        p = self.pending.get(h)
        if not p:
            return None, [], "it isn't waiting for cleanup"
        if not p.undo:
            return None, [], "it was queued before Undo existed"
        rows: list[UndoRow] = [(h, p.name, p.undo)]
        if p.undo["kind"] == "series" and p.event:
            rows += [(x.hash, x.name, x.undo) for x in self.pending.by_event(p.event, h)]
        t = self.connections.torrents.get(h)
        if not t:
            return None, rows, "its torrent is already gone"
        how = restorable(t["content_path"])
        if not how:
            return t, rows, "there's no video or RAR archive in the torrent to restore from"
        if how == "rar" and not self.config.load().restore_enabled:
            return t, rows, "it's a RAR release and RAR restore is turned off in Settings"
        return t, rows, None

    def steps(self, t: Torrent, rows: list[UndoRow]) -> list[str]:
        snap = rows[0][2] or {}
        c = self.config.load()
        app = c.arr_name(snap_app(snap))
        if snap["kind"] == "movie":
            first = f"Re-add '{snap['movie']['title']}' to {app} with its previous settings (if it was removed)"
        elif snap["kind"] == "series":
            first = f"Re-add '{snap['series']['title']}' to {app} with its previous settings and monitoring"
        else:
            first = f"Monitor {rows[0][1]} again in {app}"
        n = len(rows)
        middle = (
            f"Have {app} re-import the file{'s' if n > 1 else ''} from "
            f"{'these torrents' if n > 1 else 'this torrent'}'s download, "
            f"so it's back in your library and {c.media_name}"
        )
        if restorable(t["content_path"]) == "rar":
            middle = (
                f"Unpack it again from the torrent's RAR files (takes a minute or two), then have {app} import it, "
                f"so it's back in your library and {c.media_name}"
            )
        return [
            f"Take {'these torrents' if n > 1 else 'the torrent'} off the cleanup list. "
            f"{'They keep' if n > 1 else 'It keeps'} seeding as normal",
            first,
            middle,
        ]

    def run(self, h: str) -> None:
        try:
            t, rows, problem = self.info(h)
        except Exception as e:  # reported below, like any other reason Undo can't run
            t, rows, problem = None, [], repr(e)
        if problem:
            return self.activity.error(f"Undo failed: {problem}")
        snap = rows[0][2] or {}
        kind = "movie" if snap["kind"] == "movie" else "tv"
        arr = self.connections.arr(snap_app(snap))
        self.activity.info(f"Undo: {rows[0][1]}")

        def step(fn: Callable[[], str]) -> bool:
            try:
                self.activity.info(f"  done: {fn()}")
                return True
            except Exception as e:  # reported; the other steps still run
                self.activity.error(f"  FAILED: {e}")
                return False

        try:
            groups = self.library.cross_seed_groups(self.connections.torrents.all(), 0)
        except ServiceError:
            groups = {}
        siblings = {x for h0, _, _ in rows for x in groups.get(h0, ())} - {x for x, _, _ in rows}
        # off the cleanup list before anything else: re-adding can take a minute, and a sweep in that time could
        # otherwise still delete them
        self.pending.remove(*[x for x, _, _ in rows])
        self.pending.remove(*siblings, source="cross-seed")
        self.activity.info(f"  done: took {len(rows)} torrent{'s' * (len(rows) != 1)} off the cleanup list")
        ok = True
        if isinstance(arr, RadarrClient) and snap["kind"] == "movie":
            ok = step(lambda: self.restorer.ensure_movie(arr, snap["movie"]))
        elif isinstance(arr, SonarrClient) and snap["kind"] == "series":
            ok = step(lambda: self.restorer.ensure_series(arr, snap))
        elif isinstance(arr, SonarrClient):
            ok = step(lambda: self.restorer.remonitor(arr, snap))

        def reimported(tt: Torrent) -> str:
            return f"re-imported '{tt['name']}': " + self.restorer.reimport(arr, kind, tt)

        def unpacked(tt: Torrent) -> str:
            return f"restored '{tt['name']}': " + self.restorer.unpack(arr, kind, tt)

        for x, name, _ in rows:
            tt = t if x == h else self.connections.torrents.get(x)
            how = restorable(tt["content_path"]) if tt else None
            if tt and how == "video":
                ok &= step(partial(reimported, tt))
            elif tt and how == "rar":
                self.activity.info(f"  unpacking '{tt['name']}'; this can take a minute or two")
                ok &= step(partial(unpacked, tt))
            else:  # gone from the client, or nothing left in it to restore from
                ok = False
                self.activity.error(f"  FAILED: nothing to restore {name} from: its torrent is gone or has no video")
        seerr = self.connections.seerr()
        if seerr:
            with contextlib.suppress(ServiceError):
                seerr.sync("radarr" if kind == "movie" else "sonarr")
        self.notifier.notify("Undo " + ("finished" if ok else "had a problem"), rows[0][1], "undo")
        return None
