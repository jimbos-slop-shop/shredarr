"""The No torrent tab: media in Sonarr/Radarr that no torrent seeds any more (so deleting it frees space for real)."""

from __future__ import annotations

import os
import time
from typing import Any

from ..clients.base import NotConfigured, ServiceHTTPError
from ..config import JSON, Config, ConfigStore, kind_of
from ..files import nlink
from ..repositories import HistoryRepo
from ..text import episodes_label, human
from .activity import Activity
from .connections import Connections
from .deletes import Suppressions
from .library import Library
from .notifications import Notifier
from .removal import MediaRemoval, Step, run_steps
from .snapshots import movie_settings, season_monitoring, series_settings

Done = tuple[str, int, JSON]  # (label, bytes freed, what Redownload needs)


class Orphans:
    def __init__(
        self,
        config: ConfigStore,
        connections: Connections,
        library: Library,
        removal: MediaRemoval,
        history: HistoryRepo,
        suppressions: Suppressions,
        activity: Activity,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.connections = connections
        self.library = library
        self.removal = removal
        self.history = history
        self.suppressions = suppressions
        self.activity = activity
        self.notifier = notifier
        self._cache: tuple[float, list[JSON]] = (0, [])

    def forget(self) -> None:
        self._cache = (0, [])

    def has_torrent(self) -> Any:
        return self.library.file_check(self.connections.torrents.all())

    def items(self, max_age: float = 300) -> tuple[float, list[JSON]]:
        """(when, items): movies and seasons whose files don't come from any torrent in the torrent client any more,
        biggest first."""
        if time.time() - self._cache[0] < max_age:
            return self._cache
        has_torrent, items = self.has_torrent(), []
        local = self.connections.paths().local
        for inst, radarr in self.connections.radarrs():
            for m in radarr.movies():
                f = m.get("movieFile")
                if m.get("hasFile") and f and not has_torrent(p := local(f["path"])):
                    items.append(
                        {
                            "id": f"{inst.key}:{m['id']}",
                            "app": inst.key,
                            "kind": inst.kind,
                            "instance": inst.name,
                            "title": m["title"],
                            "year": m.get("year"),
                            "what": "Movie",
                            "size": f.get("size") or 0,
                            "files": 1,
                            "linked": nlink(p) > 1,
                            "path": p,
                        }
                    )
        for inst, sonarr in self.connections.sonarrs():
            for s in sonarr.all_series():
                if not (s.get("statistics") or {}).get("episodeFileCount"):
                    continue
                lone = {f["id"]: f for f in sonarr.episode_files(s["id"]) if not has_torrent(local(f["path"]))}
                if not lone:
                    continue
                eps = [e for e in sonarr.episodes(s["id"]) if e.get("episodeFileId") in lone]
                for sn in sorted({f["seasonNumber"] for f in lone.values()}):
                    fs = [f for f in lone.values() if f["seasonNumber"] == sn]
                    items.append(
                        {
                            "id": f"{inst.key}:{s['id']}:{sn}",
                            "app": inst.key,
                            "kind": inst.kind,
                            "instance": inst.name,
                            "title": s["title"],
                            "year": s.get("year"),
                            "what": episodes_label([e for e in eps if e["seasonNumber"] == sn]) or f"Season {sn}",
                            "size": sum(f.get("size") or 0 for f in fs),
                            "files": len(fs),
                            "linked": any(nlink(local(f["path"])) > 1 for f in fs),
                            "path": os.path.dirname(local(fs[0]["path"])),
                        }
                    )
        items.sort(key=lambda x: -x["size"])
        self._cache = (time.time(), items)
        return self._cache

    def plan(self, c: Config, ids: list[str]) -> tuple[list[Step], list[Done], set[str]]:
        """Delete steps for items from items() (checked again, in case a torrent showed up meanwhile).
        Returns (steps, [(label, size, restore info)], library paths)."""
        has_torrent = self.has_torrent()
        local = self.connections.paths().local
        paths: set[str] = set()
        movies: list[tuple[str, JSON]] = []
        series: list[tuple[str, JSON, list[JSON]]] = []
        done: list[Done] = []
        for i in ids:
            app, *rest = str(i).split(":")
            if kind_of(app) == "radarr" and rest:
                try:
                    m = self.connections.radarr(app).movie(int(rest[0]))
                except (ServiceHTTPError, NotConfigured):  # already gone (or that Radarr was removed): skip it
                    continue
                f = m.get("movieFile")
                if m.get("hasFile") and f and not has_torrent(p := local(f["path"])):
                    paths.add(p)
                    movies.append((app, m))
                    done.append(
                        (
                            m["title"],
                            (f.get("size") or 0) if nlink(p) <= 1 else 0,  # a file with other links frees nothing
                            {"app": app, "movie": movie_settings(m), "episodes": []},
                        )
                    )
            elif kind_of(app) == "sonarr" and len(rest) == 2:
                sid, sn = int(rest[0]), int(rest[1])
                try:
                    sonarr = self.connections.sonarr(app)
                    s, files, all_eps = sonarr.series(sid), sonarr.episode_files(sid), sonarr.episodes(sid)
                except (ServiceHTTPError, NotConfigured):  # already gone (or that Sonarr was removed): skip it
                    continue
                lone = {f["id"]: f for f in files if f["seasonNumber"] == sn and not has_torrent(local(f["path"]))}
                eps = [e for e in all_eps if e.get("episodeFileId") in lone]
                if eps:
                    paths |= {local(f["path"]) for f in lone.values()}
                    series.append((app, s, eps))
                    done.append(
                        (
                            f"{s['title']} {episodes_label(eps)}",
                            sum(f.get("size") or 0 for f in lone.values() if nlink(local(f["path"])) <= 1),
                            {
                                "app": app,
                                "series": {**series_settings(s), "seasons": season_monitoring(s)},
                                "episodes": sorted([[e["seasonNumber"], e["episodeNumber"]] for e in eps]),
                            },
                        )
                    )
        return self.removal.steps(c, paths, movies, series), done, paths

    def delete(self, ids: list[str]) -> None:
        c = self.config.load()
        try:
            steps, done, paths = self.plan(c, ids)
        except Exception as e:  # nothing was done yet; reported
            return self.activity.error(f"Deleting media with no torrent failed while planning: {e!r}")
        if not c.dry_run:
            self.suppressions.add_paths(paths)
        self.activity.info(f"Delete media with no torrent: {', '.join(x[0] for x in done) or 'nothing left to delete'}")
        run_steps(steps, c.dry_run, self.activity)
        if not c.dry_run and done:
            for label, size, restore in done:
                self.history.add(
                    label,
                    size,
                    title=label,
                    category=None,
                    reason="no torrent",
                    h=None,
                    restore={"hash": None, "name": label, "category": None, "movie": None, "series": None, **restore},
                )
            self.forget()
            self.library.forget(roots=False, index=True, imports=True, groups=False, titles=False)
            self.notifier.notify(
                "Deleted media with no torrent",
                f"{', '.join(x[0] for x in done)} · {human(sum(x[1] for x in done))} freed",
                "cleanup",
            )
        return None
