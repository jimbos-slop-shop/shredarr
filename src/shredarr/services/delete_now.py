"""Delete now: a torrent, its cross-seeds and its media, everywhere, right away (the seeding rule is ignored)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial

from ..clients.base import NotConfigured, ServiceHTTPError
from ..clients.torrents import Torrent
from ..config import Config, ConfigStore, kind_of
from ..files import file_key
from ..repositories import OverridesRepo, PendingRepo
from ..text import episodes_label, human
from .activity import Activity
from .cleanup import Cleaner
from .connections import Connections
from .deletes import Suppressions
from .library import Library
from .media_server import media_server
from .notifications import Notifier
from .removal import MediaRemoval, Step
from .trackers import tracker_host


@dataclass
class Plan:
    torrent: Torrent | None
    steps: list[Step]
    group: list[str] = field(default_factory=list)  # the torrent and its cross-seeds, still in the client
    ts: dict[str, Torrent] = field(default_factory=dict)  # every torrent, as it was when planned
    freed: dict[str, int] = field(default_factory=dict)  # filled in as torrents are deleted
    paths: set[str] = field(default_factory=set)  # the library files that go


class DeleteNow:
    def __init__(
        self,
        config: ConfigStore,
        connections: Connections,
        library: Library,
        removal: MediaRemoval,
        cleaner: Cleaner,
        pending: PendingRepo,
        overrides: OverridesRepo,
        suppressions: Suppressions,
        activity: Activity,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.connections = connections
        self.library = library
        self.removal = removal
        self.cleaner = cleaner
        self.pending = pending
        self.overrides = overrides
        self.suppressions = suppressions
        self.activity = activity
        self.notifier = notifier

    def plan(self, c: Config, h: str) -> Plan:
        """Everything Delete now would remove for torrent h. Only movies and episodes whose current file came from this
        torrent (or its cross-seeds) are touched: if Sonarr/Radarr has since upgraded one to a file from another
        download, it's left alone (a note says so)."""
        ts = {x["hash"]: x for x in self.connections.torrents.all()}
        t = ts.get(h)
        group = sorted(
            self.library.cross_seed_groups(list(ts.values()), 0).get(h, {h}) if t else {h}, key=lambda x: x != h
        )
        imap = self.library.imports(0)
        imps = [i for x in group for i in imap.get(x, [])]
        keys = set().union(*[self.library.torrent_keys(ts[x]) for x in group if x in ts])
        idx = self.library.index(0)
        owner = self.library.import_owner(0)
        paths = {p for k, p in idx.items() if k in keys} | {
            i["path"] for i in imps if i["path"] and owner.get(i["path"]) in group and os.path.exists(i["path"])
        }
        verify = self.library.index_ok  # library folders readable, so file ownership can be checked
        local = self.connections.paths().local
        media = c.media_name

        def from_elsewhere(p: str | None) -> bool:
            """True if this library file exists and isn't one of this torrent's."""
            return verify and bool(p) and os.path.exists(p or "") and file_key(p or "") not in keys and p not in paths

        notes: list[Step] = []
        movies, series = [], []
        for key, mid in sorted(
            {(i["app"], i["movieId"]) for i in imps if kind_of(i["app"]) == "radarr" and i["movieId"]}
        ):
            try:
                m = self.connections.radarr(key).movie(mid)
            except (ServiceHTTPError, NotConfigured):
                continue
            if m.get("hasFile") and from_elsewhere(local((m.get("movieFile") or {}).get("path"))):
                notes.append(
                    Step(
                        f"Keep '{m['title']}' in {c.arr_name(key)} and {media}: "
                        "its current file came from a different download"
                    )
                )
            else:
                movies.append((key, m))
        by_series: dict[tuple[str, int], set[int]] = {}
        for i in imps:
            if kind_of(i["app"]) == "sonarr" and i["episodeId"]:
                by_series.setdefault((i["app"], i["seriesId"]), set()).add(i["episodeId"])
        for (key, sid), epids in by_series.items():
            try:
                sonarr = self.connections.sonarr(key)
                s = sonarr.series(sid)
            except (ServiceHTTPError, NotConfigured):
                continue
            eps = [e for e in sonarr.episodes(sid) if e["id"] in epids]
            fpath = (
                {f["id"]: local(f["path"]) for f in sonarr.episode_files(sid)}
                if any(e.get("episodeFileId") for e in eps)
                else {}
            )
            other = [e for e in eps if e.get("episodeFileId") and from_elsewhere(fpath.get(e["episodeFileId"]))]
            if other:
                notes.append(
                    Step(
                        f"Keep '{s['title']}' {episodes_label(other)} in {c.arr_name(key)} and {media}: "
                        f"{'their current files' if len(other) > 1 else 'its current file'} "
                        "came from a different download"
                    )
                )
            mine = [e for e in eps if e not in other]
            if mine:
                series.append((key, s, mine))
        plan = Plan(
            t, self.removal.steps(c, paths, movies, series) + notes, [x for x in group if x in ts], dict(ts), {}, paths
        )

        def delete_torrent(x: str) -> None:
            plan.freed.update(self.cleaner.remove_torrents([x], ts)[0])

        for x in group:
            if x in ts:
                what = "cross-seed torrent" if x != h else "torrent"
                host = tracker_host(ts[x]) if x != h else ""
                plan.steps.append(
                    Step(
                        f"Delete {what} '{ts[x]['name']}'"
                        + (f" ({host})" if host else "")
                        + f" and its downloaded data from {c.client_name}",
                        partial(delete_torrent, x),
                    )
                )
        if t and not imps:  # not managed by Sonarr/Radarr (e.g. courses the media server reads straight from downloads)
            plan.steps.append(
                Step(
                    f"Rescan the {media} library so it drops the deleted files",
                    media_server(self.connections).refresh_all,
                )
            )
        return plan

    def run(self, h: str) -> None:
        c = self.config.load()
        try:
            plan = self.plan(c, h)
        except Exception as e:
            return self.activity.error(f"Delete now failed while planning: {e!r}")
        t = plan.torrent
        self.activity.info(f"Delete now: {t['name'] if t else h}")
        restores = (
            {
                x: ({"cross_seed_of": t["name"]} if x != h and t else self.cleaner.capture_restore(plan.ts[x]))
                for x in plan.group
            }
            if not c.dry_run
            else {}
        )
        if not c.dry_run:
            self.suppressions.add_paths(plan.paths)
        for step in plan.steps:
            if step.run is None:
                self.activity.info(f"  {step.desc}")
            elif c.dry_run:
                self.activity.info(f"  [dry run] would: {step.desc}")
            else:
                try:
                    step.run()
                    self.activity.info(f"  done: {step.desc}")
                except Exception as e:  # one failed step shouldn't stop the others
                    self.activity.error(f"  FAILED: {step.desc}: {e}")
        if c.dry_run:
            return None
        self.pending.remove(*(plan.group or [h]))
        self.overrides.remove(*(plan.group or [h]))
        for x in plan.group:
            if x in plan.freed and t:
                self.cleaner.record(plan.ts[x], t["name"], "Delete now", restores.get(x), plan.freed[x])
        self.library.forget(roots=False, titles=False)
        if t:
            self.notifier.notify("Deleted now", f"{t['name']} · {human(sum(plan.freed.values()))} freed", "cleanup")
        return None
