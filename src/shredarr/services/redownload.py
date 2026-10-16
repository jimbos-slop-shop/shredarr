"""History → Redownload: get media back after its torrent was deleted for good, either by adding the saved .torrent
again ("same release") or by having Sonarr/Radarr search for a new release."""

from __future__ import annotations

import os
import re
import time

from ..clients.base import ServiceError
from ..clients.servarr import RadarrClient, SonarrClient
from ..config import ARR_KINDS, JSON, ConfigStore, kind_of
from ..repositories import HistoryRepo, RedownloadsRepo
from ..text import episodes_text
from .activity import Activity
from .connections import Connections
from .library import Library
from .media_server import media_server
from .notifications import Notifier
from .restore import Restorer, wait_for

NUDGE_AFTER = 600  # seconds a finished re-added torrent may take to be imported before Shredarr helps
NUDGE_TRIES = 3


class Redownloader:
    def __init__(self, config: ConfigStore, connections: Connections, library: Library, restorer: Restorer,
                 history: HistoryRepo, redownloads: RedownloadsRepo, activity: Activity, notifier: Notifier,
                 torrent_dir: str) -> None:
        self.config = config
        self.connections = connections
        self.library = library
        self.restorer = restorer
        self.history = history
        self.redownloads = redownloads
        self.activity = activity
        self.notifier = notifier
        self.torrent_dir = torrent_dir

    def saved_torrent(self, h: str | None) -> str | None:
        p = os.path.join(self.torrent_dir, (h or "") + ".torrent")
        return p if h and os.path.isfile(p) else None

    def resolve(self, row_id: int) -> JSON:
        """What a History entry was, as {app, movie, series, episodes, name, category, hash}. Entries saved before
        Shredarr kept this are worked out from the torrent name with Sonarr/Radarr's own parser."""
        row = self.history.get(row_id)
        if not row:
            raise ServiceError("that History entry doesn't exist")
        if row.restore:
            info = dict(row.restore)
            if info.get("cross_seed_of"):
                raise ServiceError(f"it was a cross-seed of '{info['cross_seed_of']}'. Redownload that one instead")
            if info.get("movie") or info.get("series"):
                if kind_of(info.get("app")) not in ARR_KINDS or not any(x.key == info["app"] for x in self.config.load().arr_instances()):
                    info["app"] = "radarr" if info.get("movie") else "sonarr"  # older entry, or that instance was removed since
                return info
        info = {"hash": row.hash, "name": row.name, "category": row.category, "movie": None, "series": None, "episodes": []}
        order = ("sonarr", "radarr") if re.search(r"tv|sonarr|show|series", row.category or "", re.I) else ("radarr", "sonarr")
        for app in order:
            try:
                arr = self.connections.arr(app)
                pr = arr.parse(row.name)
            except ServiceError:
                continue
            if isinstance(arr, RadarrClient) and (pr.get("movie") or (pr.get("parsedMovieInfo") or {}).get("movieTitles")):
                m = pr.get("movie")
                if not m:
                    pm = pr["parsedMovieInfo"]
                    found = arr.lookup(f"{pm['movieTitles'][0]} {pm.get('year') or ''}".strip())
                    m = found[0] if found else None
                if m:
                    info.update(app="radarr", movie={"tmdbId": m["tmdbId"], "title": m["title"], "year": m.get("year"), "monitored": True,
                                                     "minimumAvailability": m.get("minimumAvailability") or "released"})
                    return info
            if isinstance(arr, SonarrClient) and pr.get("parsedEpisodeInfo"):
                pe = pr["parsedEpisodeInfo"]
                sr = pr.get("series")
                if not sr:
                    found = arr.lookup(pe.get("seriesTitle") or row.title or row.name)
                    sr = found[0] if found else None
                if sr:
                    info.update(app="sonarr", series={"tvdbId": sr["tvdbId"], "title": sr["title"], "monitored": True, "seasonFolder": True,
                                                      "seasons": []},
                                episodes=[[pe["seasonNumber"], n] for n in pe.get("episodeNumbers") or []],
                                full_seasons=[pe["seasonNumber"]] if pe.get("fullSeason") else [])
                    return info
        raise ServiceError("Shredarr couldn't tell which movie or show this was")

    def plan(self, row_id: int) -> JSON:
        info = self.resolve(row_id)
        c = self.config.load()
        app = c.arr_name(info["app"])
        if kind_of(info["app"]) == "radarr":
            what = f"'{info['movie']['title']}'" + (f" ({info['movie']['year']})" if info["movie"].get("year") else "")
            first = f"Put {what} back in {app} (with its previous settings if Shredarr has them) and monitor it"
            search = f"Have {app} search for it and grab the best release it finds"
        else:
            eps = episodes_text(info.get("episodes") or [])
            full = info.get("full_seasons") or []
            what = f"'{info['series']['title']}'" + (f" {eps}" if eps else (f" season {', '.join(map(str, full))}" if full else ""))
            first = f"Put '{info['series']['title']}' back in {app} if it was removed, and monitor {eps or 'those episodes'}"
            search = f"Have {app} search for " + (eps or "them") + " (whole seasons as season packs)"
        done = f"When it finishes, it's imported into your library and shows up in {c.media_name}"
        return {"what": what, "app": info["app"], "same_available": bool(self.saved_torrent(info.get("hash"))),
                "steps": {"same": [first, f"Add the original torrent back to {c.client_name} ({info['name']}), so the same release downloads again", done],
                          "search": [first, search, done]}}

    def run(self, row_id: int, how: str) -> None:
        try:
            info = self.resolve(row_id)
        except Exception as e:  # reported in the activity log
            return self.activity.error(f"Redownload failed: {e}")
        is_movie = kind_of(info["app"]) == "radarr"
        title = info["movie"]["title"] if is_movie else info["series"]["title"]
        self.activity.info(f"Redownload: {title}" + ("" if is_movie else f" {episodes_text(info.get('episodes') or [])}"))
        try:
            arr = self.connections.arr(info["app"])
            if isinstance(arr, RadarrClient):
                self.activity.info("  done: " + self.restorer.ensure_movie(arr, info["movie"]))
                m = arr.movies(info["movie"]["tmdbId"])[0]
                if not m["monitored"]:
                    m["monitored"] = True
                    arr.update_movie(m)
                movie_ids = [m["id"]]
            else:
                assert isinstance(arr, SonarrClient)
                series_id, episode_ids, whole, full_series = self.monitor_episodes(arr, info)
            if how == "same":
                path = self.saved_torrent(info.get("hash")) or os.path.join(self.torrent_dir, (info.get("hash") or "") + ".torrent")
                with open(path, "rb") as f:
                    content = f.read()
                self.connections.torrents.add(content, os.path.basename(path), info.get("category") or "")
                self.redownloads.add(info["hash"], info["app"], title)
                self.activity.info(f"  done: added the original torrent back to {self.config.load().client_name} ({info['name']})")
            elif isinstance(arr, RadarrClient):
                arr.command("MoviesSearch", movieIds=movie_ids)
                self.activity.info("  done: asked Radarr to search for it")
            elif full_series:
                arr.command("SeriesSearch", seriesId=series_id)
                self.activity.info("  done: asked Sonarr to search for the whole series")
            else:
                assert isinstance(arr, SonarrClient)
                for n in whole:
                    arr.command("SeasonSearch", seriesId=series_id, seasonNumber=n)
                in_whole = {e["id"] for e in arr.episodes(series_id) if e["seasonNumber"] in whole}
                rest = [i for i in episode_ids if i not in in_whole]
                if rest:
                    arr.command("EpisodeSearch", episodeIds=rest)
                self.activity.info("  done: asked Sonarr to search for " + (f"season {', '.join(map(str, whole))}" if whole else "")
                                   + (" and " if whole and rest else "") + (f"{len(rest)} episode{'s' * (len(rest) != 1)}" if rest else ""))
            seerr = self.connections.seerr()
            if seerr:
                try:
                    seerr.sync(kind_of(info["app"]))
                except ServiceError:
                    pass
            self.history.mark_redownloaded(row_id, how)
            self.notifier.notify("Redownload started", f"{title}: " + ("re-adding the original torrent" if how == "same" else "searching for a release"),
                                 "undo")
        except Exception as e:  # reported in the activity log
            self.activity.error(f"  FAILED: redownload of {title}: {e}")
        return None

    def monitor_episodes(self, sonarr: SonarrClient, info: JSON) -> tuple[int, list[int], list[int], bool]:
        """Put the show back if needed and monitor the episodes. Returns (series id, episode ids, whole seasons,
        whether that's the whole show)."""
        sn = info["series"]
        snap = {"series": {k: v for k, v in sn.items() if k != "seasons"}, "seasons": sn.get("seasons") or [], "monitored_eps": []}
        self.activity.info("  done: " + self.restorer.ensure_series(sonarr, snap))
        sr = next(x for x in sonarr.all_series() if x["tvdbId"] == sn["tvdbId"])
        eps = wait_for(lambda: sonarr.episodes(sr["id"]), timeout=90) or []
        want = {tuple(e) for e in info.get("episodes") or []}
        full = set(info.get("full_seasons") or [])
        ids = [e["id"] for e in eps if (e["seasonNumber"], e["episodeNumber"]) in want or e["seasonNumber"] in full]
        if ids:
            sonarr.set_monitored(ids, True)
        seasons = sorted({e["seasonNumber"] for e in eps if e["id"] in ids})
        whole = [n for n in seasons if all(e["id"] in ids for e in eps if e["seasonNumber"] == n and e.get("airDateUtc"))]
        aired_seasons = {e["seasonNumber"] for e in eps if e["seasonNumber"] > 0 and e.get("airDateUtc")}
        full_series = bool(seasons) and set(whole) == aired_seasons
        if whole:
            sonarr.set_seasons_monitored(sr["id"], set(whole), True)
        self.activity.info(f"  done: monitored {len(ids)} episode{'s' * (len(ids) != 1)} in Sonarr")
        return sr["id"], ids, whole, full_series

    def check(self) -> None:
        """Re-added torrents are normally imported by Sonarr/Radarr on their own. If one has finished but still isn't
        in the library after a while, nudge the import (or unpack a RAR release first)."""
        for r in self.redownloads.open():
            t = self.connections.torrents.get(r.hash)
            if not t:
                if time.time() - r.ts > 3600:
                    self.redownloads.done(r.hash)
                continue
            self.library.forget_torrent(r.hash)
            self.library.forget(roots=False, index=False, groups=False, titles=False)
            if self.library.in_library(t):
                self.redownloads.done(r.hash)
                media_server(self.connections).rescan([i["path"] for i in self.library.imports(0).get(r.hash, [])
                                                       if i["path"] and os.path.exists(i["path"])])
                self.activity.info(f"Redownload finished: '{r.title}' is back in your library")
                self.notifier.notify("Redownload finished", f"{r.title} is back in your library", "undo")
                continue
            if t["progress"] < 1 or time.time() - max(r.ts, r.last_try or 0) < NUDGE_AFTER or r.tries >= NUDGE_TRIES:
                continue
            self.redownloads.tried(r.hash)
            arr = self.connections.arr(r.app)
            try:
                how = self.restorer.restore(arr, "movie" if kind_of(r.app) == "radarr" else "tv", t)
                self.activity.info(f"Redownload of '{r.title}' imported: {how}")
            except Exception as e:  # tried again next time
                self.activity.info(f"Redownload of '{r.title}': import not done yet ({e})")
