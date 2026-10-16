"""Media deleted in the media server: clean it up in Sonarr, Radarr and Seerr right away, and queue its torrents.

Jellyfin reports deletes through its Webhook plugin. Plex can't, so with Plex the library watch (watcher.py) reports
files that disappeared from disk instead (disk_deletes).
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from ..clients.base import ServiceError
from ..clients.jellyfin import jf_id
from ..clients.servarr import RadarrClient, SonarrClient
from ..config import JSON, Config, ConfigStore
from ..files import PathMapper
from ..text import episodes_label
from .activity import Activity
from .connections import Connections
from .notifications import Notifier
from .queue import CleanupQueue
from .snapshots import movie_settings, season_monitoring, series_settings


class Suppressions:
    """Deletes Shredarr made itself (Delete now, No torrent), so they aren't handled again when the media server
    reports them."""

    def __init__(self) -> None:
        self._jellyfin: set[str] = set()
        self._paths: dict[str, float] = {}
        self._lock = threading.Lock()

    def add_jellyfin_item(self, item_id: str) -> None:
        with self._lock:
            self._jellyfin.add(jf_id(item_id))

    def jellyfin_item(self, item_id: str | None) -> bool:
        return jf_id(item_id) in self._jellyfin

    def add_paths(self, paths: set[str]) -> None:
        now = time.time()
        with self._lock:
            for p in paths:
                self._paths[p] = now
            for p in [p for p, t in self._paths.items() if now - t > 86400]:
                self._paths.pop(p, None)

    def path(self, p: str) -> bool:
        return p in self._paths


def find_series(sonarr: SonarrClient, tvdb: Any = None, tmdb: Any = None, imdb: Any = None, title: str | None = None) -> JSON | None:
    series = sonarr.all_series()
    for key, val in (("tvdbId", tvdb), ("tmdbId", tmdb), ("imdbId", imdb)):
        hits = [s for s in series if val and str(s.get(key)) == str(val)]
        if hits:
            return hits[0]
    hits = [s for s in series if title and s["title"].casefold() == title.casefold()]
    return hits[0] if len(hits) == 1 else None


def season_folder_pattern(season: int) -> str:
    return rf"Season 0*{season}" + ("|Specials" if season == 0 else "")


class MediaDeletes:
    def __init__(self, config: ConfigStore, connections: Connections, queue: CleanupQueue, activity: Activity,
                 notifier: Notifier, suppressions: Suppressions) -> None:
        self.config = config
        self.connections = connections
        self.queue = queue
        self.activity = activity
        self.notifier = notifier
        self.suppressions = suppressions

    @property
    def paths(self) -> PathMapper:
        return self.connections.paths()

    def act(self, c: Config, desc: str, fn: Callable[[], Any]) -> None:
        """Do one cleanup step (or, in dry run, only say what it would have done)."""
        if c.dry_run:
            return self.activity.info(f"  [dry run] would have {desc}")
        try:
            fn()
            self.activity.info(f"  {desc}")
        except Exception as e:  # one failed step shouldn't stop the others
            self.activity.error(f"  FAILED: {desc}: {e}")

    # ------------------------------------------------------------------ Jellyfin webhook events
    def handle(self, ev: JSON) -> None:
        if ev.get("disk_deletes"):
            return self.disk_deletes(ev["disk_deletes"])
        if self.suppressions.jellyfin_item(ev.get("item_id")):  # deleted by Shredarr itself; already handled
            return None
        c = self.config.load()
        kind, sn, ep = ev.get("item_type"), ev.get("season_number"), ev.get("episode_number")
        who = " / ".join(x for x in (ev.get("series_name"), ev.get("name")) if x)
        self.activity.info(f"Jellyfin deleted {kind}: {who}" + (f" (S{sn}" + (f"E{ep}" if ep is not None else "") + ")" if sn is not None else ""))
        if kind in ("Movie", "Series", "Season", "Episode"):
            self.notifier.notify("Deleted in Jellyfin", f"{kind}: {who}", "jellyfin")
        if kind == "Movie":
            self.on_movie(c, ev)
        elif kind == "Series":
            self.on_series(c, ev)
        elif kind == "Season" and sn is not None:
            self.on_episodes(c, ev, sn)
        elif kind == "Episode" and sn is not None and ep is not None:
            self.on_episodes(c, ev, sn, range(ep, (ev.get("episode_end_number") or ep) + 1))
        else:
            self.activity.info("  ignored")
        return None

    def on_movie(self, c: Config, ev: JSON) -> None:
        """With more than one Radarr (say a 4K one), only the instances whose copy is really gone are cleaned up."""
        found = []
        for inst, radarr in self.connections.arrs("radarr"):
            assert isinstance(radarr, RadarrClient)
            movies = radarr.movies()
            m = next((m for m in movies if ev.get("tmdb_id") and m.get("tmdbId") == ev["tmdb_id"]), None) \
                or next((m for m in movies if ev.get("imdb_id") and m.get("imdbId") == ev["imdb_id"]), None)
            if not m:
                hits = [m for m in movies if m["title"].casefold() == (ev.get("name") or "").casefold()]
                m = hits[0] if len(hits) == 1 else None
            if m:
                kept = bool(m.get("hasFile")) and self.paths.on_disk((m.get("movieFile") or {}).get("path"))
                found.append((inst.key, inst.name, radarr, m, kept))
        if not found:
            return self.activity.info("  no Radarr match")
        for key, name, radarr, m, kept in found:
            if kept:
                self.activity.info(f"  '{m['title']}' still has its file in {name}, so that copy stays")
            else:
                self.movie_removed(c, key, name, radarr, m, seerr=not any(x[4] for x in found))
        return None

    def movie_removed(self, c: Config, key: str, name: str, radarr: RadarrClient, m: JSON, seerr: bool = True) -> None:
        hashes = list(dict.fromkeys(h["downloadId"] for h in radarr.movie_history(m["id"]) if h.get("downloadId")))
        snap = {"kind": "movie", "app": key, "movie": movie_settings(m)}
        self.queue.add(hashes, m["title"], "movie", snap, secrets.token_hex(6))
        excl = c.behavior["import_exclusion"]
        self.act(c, f"removed '{m['title']}' from {name}" + (" (with import exclusion)" if excl else ""),
                 lambda: radarr.delete_movie(m["id"], delete_files=True, add_exclusion=excl))
        if c.behavior["remove_from_seerr"] and seerr:
            self.seerr_remove(c, "movie", m.get("tmdbId"), m["title"])

    def on_series(self, c: Config, ev: JSON) -> None:
        found = []
        for inst, sonarr in self.connections.arrs("sonarr"):
            assert isinstance(sonarr, SonarrClient)
            s = find_series(sonarr, ev.get("tvdb_id"), ev.get("tmdb_id"), ev.get("imdb_id"), ev.get("name"))
            if s:
                found.append((inst.key, inst.name, sonarr, s, os.path.isdir(self.paths.local(s["path"]))))
        if not found:
            return self.activity.info("  no Sonarr match")
        for key, name, sonarr, s, kept in found:
            if kept:
                # Deleting a show in Jellyfin deletes its folder. If the folder is still there, Jellyfin only dropped a
                # show with no episodes left on disk (e.g. after its last episode was deleted), so keep it for new episodes.
                self.activity.info(f"  '{s['title']}' folder still exists in {name}, so this was just an empty show being dropped; keeping it")
            else:
                self.series_removed(c, key, name, sonarr, s, seerr=not any(x[4] for x in found))
        return None

    def series_removed(self, c: Config, key: str, name: str, sonarr: SonarrClient, s: JSON, seerr: bool = True) -> None:
        hashes = list(dict.fromkeys(h["downloadId"] for h in sonarr.series_history(s["id"]) if h.get("downloadId")))
        snap = {"kind": "series", "app": key, "series": series_settings(s), "seasons": season_monitoring(s),
                "monitored_eps": [[e["seasonNumber"], e["episodeNumber"]] for e in sonarr.episodes(s["id"]) if e["monitored"]]}
        self.queue.add(hashes, s["title"], "series", snap, secrets.token_hex(6))
        excl = c.behavior["import_exclusion"]
        self.act(c, f"removed '{s['title']}' from {name}" + (" (with import list exclusion)" if excl else ""),
                 lambda: sonarr.delete_series(s["id"], delete_files=True, add_exclusion=excl))
        if c.behavior["remove_from_seerr"] and seerr:
            self.seerr_remove(c, "tv", s.get("tmdbId"), s["title"])

    def on_episodes(self, c: Config, ev: JSON, season: int, numbers: range | None = None) -> None:
        """Whole season when numbers is None, otherwise the listed episode numbers."""
        ids, found = self.jellyfin_series_ids(ev), False
        for inst, sonarr in self.connections.arrs("sonarr"):
            assert isinstance(sonarr, SonarrClient)
            s = find_series(sonarr, ids.get("tvdb"), ids.get("tmdb"), ids.get("imdb"), ev.get("series_name"))
            if s:
                found = True
                self.episodes_deleted(c, inst.key, inst.name, sonarr, s, ev, season, numbers)
        if not found:
            self.activity.info("  no Sonarr match for the series")

    def jellyfin_series_ids(self, ev: JSON) -> dict[str, str]:
        """Provider ids (tvdb, tmdb, imdb) of the show an episode or season event belongs to, from Jellyfin."""
        if not ev.get("series_id"):
            return {}
        try:
            return self.connections.jellyfin().provider_ids(ev["series_id"])
        except (ServiceError, KeyError) as e:
            self.activity.error(f"  Jellyfin series lookup failed ({e}); matching by title")
        return {}

    def episodes_deleted(self, c: Config, key: str, name: str, sonarr: SonarrClient, s: JSON, ev: JSON, season: int,
                         numbers: range | None) -> None:
        folder = self.paths.local(s["path"])
        dirs = os.listdir(folder) if os.path.isdir(folder) else []
        season_dirs = [d for d in dirs if re.fullmatch(r"Season \d+|Specials", d, re.I)]
        if numbers is None and any(re.fullmatch(season_folder_pattern(season), d, re.I) for d in season_dirs):
            # same idea as on_series: a deleted season loses its folder; an emptied one doesn't
            return self.activity.info(f"  '{s['title']}' season {season} folder still exists, so Jellyfin just dropped an empty season; leaving it monitored")
        eps = [e for e in sonarr.episodes(s["id"]) if e["seasonNumber"] == season]
        flat = numbers is None and not season_dirs  # episodes sit directly in the show folder
        if flat:
            # no season folders, so a deleted season can't be told apart from an emptied one: only unmonitor what has aired
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            eps = [e for e in eps if e.get("airDateUtc") and e["airDateUtc"] <= now]
        if numbers is not None:
            eps = [e for e in eps if ev.get("tvdb_id") and e.get("tvdbId") == ev["tvdb_id"]] or [e for e in eps if e["episodeNumber"] in numbers]
        if eps and any(e.get("episodeFileId") for e in eps):
            fpath = {f["id"]: f["path"] for f in sonarr.episode_files(s["id"])}
            kept = [e for e in eps if e.get("episodeFileId") and self.paths.on_disk(fpath.get(e["episodeFileId"]))]
            if kept:
                self.activity.info(f"  '{s['title']}' {episodes_label(kept)} still {'have their files' if len(kept) > 1 else 'has its file'} "
                                   f"in {name}, so {'those copies stay' if len(kept) > 1 else 'that copy stays'}")
                eps = [e for e in eps if e not in kept]
                if not eps:
                    return None
        if not eps:
            return self.activity.info(f"  no matching episodes in {name} for '{s['title']}'")
        label = f"'{s['title']}' S{season:02d}" + ("" if numbers is None else "".join(f"E{e['episodeNumber']:02d}" for e in eps))
        self.remove_episodes(c, key, name, sonarr, s, eps, label, season if numbers is None and not flat else None)
        return None

    def remove_episodes(self, c: Config, key: str, name: str, sonarr: SonarrClient, s: JSON, eps: list[JSON], label: str,
                        season: int | None = None) -> None:
        """Episodes deleted from the library: queue their torrents, unmonitor them (and the season, when a whole season
        with its folder went) and drop their file records. The show stays for new episodes."""
        ids = sorted(e["id"] for e in eps)
        hashes = list(dict.fromkeys(h["downloadId"] for h in sonarr.series_history(s["id"])
                                    if h.get("downloadId") and h.get("episodeId") in ids))
        season_was = season is not None and any(x["seasonNumber"] == season and x["monitored"] for x in s["seasons"])
        snap = {"kind": "episodes", "app": key, "series_id": s["id"], "title": s["title"],
                "remonitor": [e["id"] for e in eps if e["monitored"]], "seasons": [season] if season_was else []}
        self.queue.add(hashes, label, "season" if season is not None else "episode", snap, secrets.token_hex(6))
        bh = c.behavior
        if bh["unmonitor"]:
            self.act(c, f"unmonitored {label} in {name}", lambda: sonarr.set_monitored(ids, False))
        if season is not None and bh["unmonitor"]:
            self.act(c, f"unmonitored season {season} of '{s['title']}'", lambda: sonarr.set_seasons_monitored(s["id"], {season}, False))
        files = sorted({e["episodeFileId"] for e in eps if e.get("episodeFileId")})
        if files and bh["remove_episode_files"]:
            self.act(c, f"removed {len(files)} episode file record(s) of {label} from {name}", lambda: sonarr.delete_episode_files(files))

    def seerr_remove(self, c: Config, kind: str, tmdb: int | None, title: str) -> None:
        seerr = self.connections.seerr()
        if not tmdb or not seerr:
            return
        info = seerr.media_info(kind, tmdb)
        if not info:
            return self.activity.info("  not in Seerr")
        self.act(c, f"removed '{title}' from Seerr", lambda: seerr.delete_media(info["id"]))
        return None

    # ------------------------------------------------------------------ Plex: files that disappeared from disk
    def disk_deletes(self, paths: list[str]) -> None:
        """Library files that were deleted outside Shredarr (in Plex): clean up the movies and episodes they were,
        unless Sonarr/Radarr already has another file for them (an upgrade)."""
        c = self.config.load()
        media = c.media_name
        for inst, arr in self.connections.arrs():
            try:
                if isinstance(arr, RadarrClient):
                    for m in arr.movies():
                        root = self.paths.local(m["path"]).rstrip("/") + "/"
                        if not any(p.startswith(root) for p in paths):
                            continue
                        if m.get("hasFile") and self.paths.on_disk((m.get("movieFile") or {}).get("path")):
                            continue  # it has a (new) file, so this was an upgrade or a stray file
                        self.activity.info(f"{media} deleted Movie: {m['title']} (noticed on disk, {inst.name})")
                        self.notifier.notify(f"Deleted in {media}", f"Movie: {m['title']}", "jellyfin")
                        others = [x for other, client in self.connections.arrs("radarr") if other.key != inst.key
                                  for x in client.movies(m["tmdbId"]) if x.get("hasFile")]  # type: ignore[union-attr]
                        self.movie_removed(c, inst.key, inst.name, arr, m, seerr=not others)
                else:
                    self.disk_episode_deletes(c, inst.key, inst.name, arr, paths)
            except Exception as e:  # one app failing shouldn't stop the others
                self.activity.error(f"  error while handling deletes in {inst.name}: {e!r}")

    def disk_episode_deletes(self, c: Config, key: str, name: str, sonarr: SonarrClient, paths: list[str]) -> None:
        media = c.media_name
        for s in sonarr.all_series():
            root = self.paths.local(s["path"]).rstrip("/") + "/"
            hit = [p for p in paths if p.startswith(root)]
            if not hit:
                continue
            files = {f["id"]: self.paths.local(f["path"]) for f in sonarr.episode_files(s["id"])}
            eps = sonarr.episodes(s["id"])
            gone: dict[int, tuple[JSON, str]] = {}
            for p in hit:
                mine = [e for e in eps if e.get("episodeFileId") and files.get(e["episodeFileId"]) == p]
                if not mine:  # Sonarr already dropped the file record: work the episodes out from the file name
                    try:
                        ids = {x["id"] for x in sonarr.parse(os.path.basename(p)).get("episodes") or []}
                        mine = [e for e in eps if e["id"] in ids]
                    except ServiceError:
                        mine = []
                for e in mine:
                    if not (e.get("episodeFileId") and os.path.exists(files.get(e["episodeFileId"]) or "")):  # no newer file
                        gone[e["id"]] = (e, p)
            if not gone:
                continue
            if not os.path.isdir(self.paths.local(s["path"])):
                self.activity.info(f"{media} deleted Series: {s['title']} (noticed on disk, {name})")
                self.notifier.notify(f"Deleted in {media}", f"Series: {s['title']}", "jellyfin")
                self.series_removed(c, key, name, sonarr, s)
                continue
            for sn in sorted({e["seasonNumber"] for e, _ in gone.values()}):
                sel = sorted((e for e, _ in gone.values() if e["seasonNumber"] == sn), key=lambda e: e["episodeNumber"])
                # a whole season: its files were in a "Season N" folder, and that folder is gone too
                folders = {os.path.dirname(p) for e, p in gone.values() if e["seasonNumber"] == sn}
                season_gone = all(re.fullmatch(season_folder_pattern(sn), os.path.basename(d), re.I) and not os.path.isdir(d)
                                  for d in folders)
                what = f"S{sn:02d}" if season_gone else episodes_label(sel)
                self.activity.info(f"{media} deleted {'Season' if season_gone else 'Episode'}: {s['title']} {what} (noticed on disk, {name})")
                self.notifier.notify(f"Deleted in {media}", f"{s['title']} {what}", "jellyfin")
                self.remove_episodes(c, key, name, sonarr, s, sel, f"'{s['title']}' {what}", sn if season_gone else None)
