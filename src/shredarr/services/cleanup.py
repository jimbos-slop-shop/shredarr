"""The cleanup sweep: deletes queued torrents (and their data) once none of their files are in the library any more and
their seeding rule is met. Runs every few minutes, after each delete, and when rules change."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from ..clients.base import ServiceError
from ..clients.torrents import Torrent
from ..config import JSON, Config, ConfigStore, Rule
from ..files import FileKey, files_under
from ..repositories import HistoryRepo, OverridesRepo, PendingRepo, RedownloadsRepo
from ..text import duration, human
from .activity import Activity
from .connections import Connections
from .library import Library
from .notifications import Notifier
from .rules import evaluate, rule_for
from .saved_torrents import SavedTorrents
from .snapshots import movie_settings, season_monitoring, series_settings
from .trackers import Trackers, tracker_host

log = logging.getLogger(__name__)
Torrents = dict[str, Torrent]
EMPTY_CLIENT_WAIT = 15 * 60  # seconds a torrent client must report no torrents before the queue is dropped


class TorrentFiles:
    """Every torrent's files as (real path, disk identity, size), read from disk once and shared by all the removals
    in a sweep. Deleting a torrent's data is only safe for files no remaining torrent uses, and without this each
    removal walked every other torrent's files again."""

    def __init__(self, ts: Torrents) -> None:
        self.files: dict[str, list[tuple[str, FileKey | None, int]]] = {}
        self.path_users: Counter[str] = Counter()  # real path -> how many torrents have it
        self.key_users: Counter[FileKey] = Counter()  # same for disk identities (hardlinks)
        for h, t in ts.items():
            out: list[tuple[str, FileKey | None, int]] = []
            for p in files_under(t["content_path"]):
                try:
                    st = os.stat(p)
                except OSError:  # gone or unreadable: it only counts by path
                    out.append((os.path.realpath(p), None, 0))
                    continue
                out.append((os.path.realpath(p), (st.st_dev, st.st_ino), st.st_size))
            self.files[h] = out
            self.path_users.update({real for real, _, _ in out})
            self.key_users.update({k for _, k, _ in out if k})

    def kept(self, going: list[str]) -> tuple[set[str], set[FileKey]]:
        """Real paths and disk identities that a torrent which isn't going still uses."""
        paths, keys = Counter[str](), Counter[FileKey]()
        for h in going:
            paths.update({real for real, _, _ in self.files.get(h, [])})
            keys.update({k for _, k, _ in self.files.get(h, []) if k})
        return (
            {p for p, n in paths.items() if self.path_users[p] > n},
            {k for k, n in keys.items() if self.key_users[k] > n},
        )

    def drop(self, h: str) -> None:
        """The torrent was removed: its files no longer keep anything."""
        out = self.files.pop(h, [])
        self.path_users.subtract({real for real, _, _ in out})
        self.key_users.subtract({k for _, k, _ in out if k})


@dataclass
class SweepContext:
    """What one sweep knows: all torrents, the cross-seed groups, what's queued, torrents that already went
    along with another one, and (once something is deleted) the torrents' files."""

    ts: Torrents
    groups: dict[str, set[str]]
    pending: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    _files: TorrentFiles | None = None

    @property
    def files(self) -> TorrentFiles:
        if self._files is None:  # most sweeps delete nothing: only read the disk when one does
            self._files = TorrentFiles(self.ts)
        return self._files


def free_gb(ts: Torrents) -> float | None:
    """Free space on the downloads disk, from the first torrent folder that exists."""
    for t in ts.values():
        if os.path.isdir(t["save_path"]):
            st = os.statvfs(t["save_path"])
            return st.f_bavail * st.f_frsize / 2**30
    return None


class Cleaner:
    def __init__(
        self,
        config: ConfigStore,
        connections: Connections,
        library: Library,
        trackers: Trackers,
        pending: PendingRepo,
        overrides: OverridesRepo,
        history: HistoryRepo,
        redownloads: RedownloadsRepo,
        activity: Activity,
        notifier: Notifier,
        saved: SavedTorrents,
    ) -> None:
        self.config = config
        self.connections = connections
        self.library = library
        self.trackers = trackers
        self.pending = pending
        self.overrides = overrides
        self.history = history
        self.redownloads = redownloads
        self.activity = activity
        self.notifier = notifier
        self.saved = saved
        self.after_sweep: list[Callable[[], None]] = []  # e.g. checking on redownloads
        self._lock = threading.Lock()
        self._after_lock = threading.Lock()  # one after-sweep run at a time, outside the sweep lock
        self._empty_since: float | None = None  # when the torrent client started reporting no torrents at all

    def rule(self, t: Torrent, overrides: dict[str, Rule], c: Config, title: str | None = None) -> Rule:
        return rule_for(t, overrides, c, title, self.trackers.names)

    # ------------------------------------------------------------------ the sweep
    def safe_sweep(self) -> None:
        try:
            self.sweep()
        except Exception as e:  # a background job: report and carry on
            self.activity.error(f"sweep failed: {e}")

    def sweep(self) -> None:
        if self._sweep():
            self._after_sweep()

    def _after_sweep(self) -> None:
        """Hooks such as the redownload check can wait minutes for an import, so they run after the sweep lock is let
        go: other sweeps (after a delete, a settings change) don't queue up behind them. If they're already running
        from an earlier sweep, this sweep skips them."""
        if not self._after_lock.acquire(blocking=False):
            return
        try:
            for fn in self.after_sweep:
                try:
                    fn()
                except Exception as e:  # reported; the sweep itself is done
                    self.activity.error(f"redownload check failed: {e}")
        finally:
            self._after_lock.release()

    def _sweep(self) -> bool:
        """One pass over the cleanup queue. Returns whether it ran (there was something to look at)."""
        with self._lock:
            c = self.config.load()
            if c.missing:
                return False
            rows = self.pending.all()
            ov = self.overrides.all()
            if not rows and not ov and not self.redownloads.open():
                return False
            ts = {t["hash"]: t for t in self.connections.torrents.all()}
            if rows and not ts and self.client_just_emptied(c):
                return True
            if ts:  # forget rules of torrents that no longer exist
                self._empty_since = None
                self.overrides.remove(*[h for h in ov if h not in ts])
            groups = self.library.cross_seed_groups(list(ts.values()), 0)
            if self.queue_cross_seeds([(p.hash, p.name) for p in rows], ts, groups):
                rows = self.pending.all()
            ctx = SweepContext(ts, groups, {p.hash for p in rows})
            for p in rows:
                if p.hash in ctx.removed:
                    self.pending.remove(p.hash)
                    continue
                t = ts.get(p.hash)
                try:
                    rule = self.rule(t, ov, c, self.library.title_of(p.hash, p.name)) if t else None
                    status = self.check(c, t, p.name, rule, p.added_at, ctx)
                except Exception as e:  # shown as this torrent's status; the others are still checked
                    status = f"error: {e}"
                if status is None:
                    self.pending.remove(p.hash)
                    ctx.pending.discard(p.hash)
                else:
                    self.pending.set_status(p.hash, status)
            self.low_space_pass(c, ctx, ov)
            return True

    def client_just_emptied(self, c: Config) -> bool:
        """A client that's starting up can briefly list no torrents at all. Dropping the whole queue (and its Undo
        data) then would be wrong, so an empty list only counts once it has stayed empty for a while."""
        now = time.time()
        if self._empty_since is None:
            self._empty_since = now
            self.activity.info(
                f"{c.client_name} reports no torrents at all; keeping the cleanup queue for now "
                "in case it's starting up"
            )
        return now - self._empty_since < EMPTY_CLIENT_WAIT

    def check(
        self,
        c: Config,
        t: Torrent | None,
        name: str,
        r: Rule | None,
        added: float | None = None,
        ctx: SweepContext | None = None,
    ) -> str | None:
        """A status for the dashboard, or None once the torrent is gone. r is the rule that applies."""
        if not t or not r:
            self.activity.info(f"torrent for {name} is gone from {c.client_name}; dropping it from the queue")
            return None
        if t["progress"] < 1:
            return "still downloading"
        ctx = ctx or SweepContext({t["hash"]: t}, {})
        ts, groups, together = ctx.ts, ctx.groups, c.behavior["cross_seed_with_original"]
        orig = self.library.cross_seed_of(t["hash"], ts, groups)
        if orig and together and orig in ctx.pending:
            return f"cross-seed: deleted together with '{ts[orig]['name']}'"
        progress, met = evaluate(r, t)
        left = self.library.in_library(t)
        if left:
            return f"waiting: {left} file(s) still in library; {progress}"
        if not met:
            return f"seeding: {progress}"
        wait = (added or 0) + c.grace_seconds - time.time()
        if added and wait > 0:
            return f"deletion delay: deletes in {duration(wait)} (Undo still possible); {progress}"
        batch = [t["hash"]]
        if together and not orig:
            batch += sorted(x for x in groups.get(t["hash"], ()) if x != t["hash"] and x in ctx.pending and x in ts)
        if c.dry_run:
            self.activity.info(
                f"[dry run] would have deleted torrent '{t['name']}' and its data ({progress})"
                + (f", with {len(batch) - 1} cross-seed{'s' * (len(batch) > 2)}" if len(batch) > 1 else "")
            )
            return f"[dry run] ready to delete; {progress}"
        for x, freed, kept in self.cleanup(batch, ts, groups, name, "seeding rule met", ctx.files):
            own = x["hash"] == t["hash"]
            self.activity.info(
                f"deleted torrent '{x['name']}'"
                + (" (its data stays: another torrent still seeds from it)" if kept else " and its data")
                + (f" ({progress})" if own else " (cross-seed, together with the original)")
            )
            self.notifier.notify(
                "Torrent cleaned up",
                f"{name}\n{x['name']} · {human(freed)} freed" + (f" ({progress})" if own else " (cross-seed)"),
                "cleanup",
            )
            if not own:
                ctx.removed.add(x["hash"])
        return None

    def queue_cross_seeds(self, rows: list[tuple[str, str]], ts: Torrents, groups: dict[str, set[str]]) -> bool:
        """Cross-seeds of torrents waiting for cleanup are cleaned up too (each by its own seeding rule, unless
        Settings say they go together with the original)."""
        queued, new = {h for h, _ in rows}, []
        for h, name in rows:
            for x in sorted(groups.get(h, ())):
                if x not in queued and x in ts:
                    queued.add(x)
                    new.append((x, name))
        for x, name in new:
            self.pending.add(x, name, "cross-seed")
        for x, name in new:
            host = tracker_host(ts[x])
            self.activity.info(
                f"  cross-seed '{ts[x]['name']}'"
                + (f" ({host})" if host else "")
                + f" of {name} queued for cleanup too"
            )
        return bool(new)

    def low_space_pass(self, c: Config, ctx: SweepContext, ov: dict[str, Rule]) -> None:
        """Settings → Low disk space: below the minimum, waiting torrents go early (oldest first) even if their seeding
        rule isn't met yet. Torrents that are still in the library, protected, or inside their deletion delay are never
        touched."""
        ls, ts = c.low_space, ctx.ts
        if not ls["enabled"]:
            return
        free = free_gb(ts)
        if free is None or free >= ls["min_free_gb"]:
            return
        for p in self.pending.all():
            t = ts.get(p.hash)
            if not t or t["progress"] < 1:
                continue
            try:
                if self.library.in_library(t):
                    continue
                if self.rule(t, ov, c, self.library.title_of(p.hash, p.name))["mode"] == "never":
                    continue
            except ServiceError:  # Sonarr/Radarr can't be asked: unsure it's out of the library or unprotected, so wait
                continue
            if p.added_at + c.grace_seconds > time.time():
                continue
            if c.dry_run:
                self.activity.info(
                    f"[dry run] low disk space ({free:.0f} GB free): would have deleted '{t['name']}' early"
                )
                continue
            self.activity.info(
                f"Low disk space ({free:.0f} GB free, minimum {ls['min_free_gb']:g} GB): deleting '{t['name']}' early"
            )
            [(_, freed, _)] = self.cleanup(
                [p.hash], ts, self.library.cross_seed_groups(list(ts.values())), p.name, "low disk space", ctx.files
            )
            self.pending.remove(p.hash)
            self.notifier.notify(
                "Torrent cleaned up early", f"Low disk space: {t['name']} · {human(freed)} freed", "cleanup"
            )
            time.sleep(3)
            free = free_gb(ts)
            if free is not None and free >= ls["min_free_gb"]:
                break

    # ------------------------------------------------------------------ deleting torrents
    def cleanup(
        self,
        batch: list[str],
        ts: Torrents,
        groups: dict[str, set[str]],
        name: str,
        reason: str,
        files: TorrentFiles | None = None,
    ) -> list[tuple[Torrent, int, bool]]:
        """Delete these torrents (one, or an original with its cross-seeds), keeping History and Redownload info.
        Returns [(torrent, bytes freed, data kept)]."""
        gone = [(ts[h], self.library.cross_seed_of(h, ts, groups)) for h in batch if h in ts]
        restores = {t["hash"]: ({"cross_seed_of": ts[o]["name"]} if o else self.capture_restore(t)) for t, o in gone}
        freed: dict[str, int] = {}
        kept: set[str] = set()
        try:
            freed, kept = self.remove_torrents([t["hash"] for t, _ in gone], ts, files)
        finally:  # if one of the client calls failed, the torrents that did go still get their History entry
            for t, _ in gone:
                if t["hash"] not in ts:
                    self.record(t, name, reason, restores[t["hash"]], freed.get(t["hash"]))
        return [(t, freed.get(t["hash"], 0), t["hash"] in kept) for t, _ in gone]

    def remove_torrents(
        self, hashes: list[str], ts: Torrents, files: TorrentFiles | None = None
    ) -> tuple[dict[str, int], set[str]]:
        """Delete torrents with their data, except when another torrent that stays still seeds from the same files (a
        cross-seed sharing the folder): then the torrent goes but the data stays. Removed torrents are taken out of ts
        (and files). Returns ({hash: bytes freed}, {hashes whose data was kept})."""
        files = files or TorrentFiles(ts)
        going = [
            h for h in dict.fromkeys(hashes) if h in ts
        ]  # in the order given: the first is credited with shared data
        keep_paths, keep_keys = files.kept(going)
        freed: dict[str, int] = {}
        with_data: list[str] = []
        without: list[str] = []
        seen: set[FileKey] = set()
        for h in going:
            mine, n = files.files.get(h, []), 0
            (without if any(real in keep_paths for real, _, _ in mine) else with_data).append(h)
            for _, k, size in mine:
                if k and k not in keep_keys and k not in seen:
                    seen.add(k)
                    n += size
            freed[h] = n if mine else ts[h]["size"]
        for group, data in ((with_data, True), (without, False)):
            if group:
                self.connections.torrents.remove(group, data)
                for h in group:  # taken out right away, so a failure in the second call leaves ts accurate
                    ts.pop(h, None)
                    files.drop(h)
        return freed, set(without)

    def capture_restore(self, t: Torrent) -> JSON:
        """Right before a torrent is deleted for good: what it was (movie / show + episodes, with Sonarr/Radarr
        settings) and a copy of its .torrent file (unless switched off), so it can be downloaded again from History."""
        h = t["hash"]
        row = self.pending.get(h)
        snap = (row.undo if row else None) or {}
        content = self.pending.content(h)
        if not content:
            try:
                content = self.library.torrent_content(h)
            except Exception:  # best effort: never stands in the way of the cleanup itself
                content = {"episodes": []}
        app = snap.get("app") or content.get("app")
        out: JSON = {
            "hash": h,
            "name": t["name"],
            "category": t.get("category"),
            "episodes": content.get("episodes") or [],
            "movie": None,
            "series": None,
            "app": app,
        }
        try:
            if snap.get("kind") == "movie":
                out["movie"] = snap["movie"]
            elif content.get("movieId"):
                out["movie"] = movie_settings(self.connections.radarr(app or "radarr").movie(content["movieId"]))
            if snap.get("kind") == "series":
                out["series"] = {**snap["series"], "seasons": snap["seasons"]}
            else:
                sid = snap.get("series_id") or content.get("seriesId")
                if sid:
                    sr = self.connections.sonarr(app or "sonarr").series(sid)
                    out["series"] = {**series_settings(sr), "seasons": season_monitoring(sr)}
        except Exception as e:  # best effort, as above
            log.warning("capture_restore: %s", e)
        if not self.saved.enabled:  # switched off in Settings: Redownload can only search again
            return out
        try:
            data = self.connections.torrents.export(h)
            if data:
                self.saved.save(h, data)
        except Exception as e:  # best effort, as above
            log.warning("torrent export: %s", e)
        return out

    def record(self, t: Torrent, name: str, reason: str, restore: JSON | None = None, size: int | None = None) -> None:
        """Add a History entry."""
        self.history.add(
            t["name"],
            t["size"] if size is None else size,
            title=self.library.title_of(t["hash"], name) or name,
            category=t.get("category"),
            reason=reason,
            h=t["hash"],
            restore=restore,
        )
