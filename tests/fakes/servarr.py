"""Fake Sonarr, Radarr and Prowlarr: just the parts of their v3/v1 APIs that Shredarr uses, backed by real files
in the lab's temporary folder (deleting through them deletes files, importing hardlinks them)."""

from __future__ import annotations

import os
import re
import shutil
from typing import TYPE_CHECKING, Any

from .server import FakeServer, Request, Response

if TYPE_CHECKING:
    from .lab import Lab

EVENT_TYPES = {1: "grabbed", 3: "downloadFolderImported", 5: "episodeFileDeleted", 6: "movieFileDeleted"}
VIDEO = (".mkv", ".mp4", ".avi")
EPISODE_RE = re.compile(r"S(\d+)E(\d+)(?:-?E(\d+))?", re.I)


def not_found(what: str) -> Response:
    return Response(404, {"message": f"{what} not found"})


class FakeServarr(FakeServer):
    """What Sonarr and Radarr share: history, root folders, commands, quality profiles, system status."""

    app = "Servarr"
    api = "/api/v3"
    version = "4.0.0.0"

    def __init__(self, lab: Lab, root_folder: str, api_key: str) -> None:
        super().__init__()
        self.name = self.app.lower()
        self.lab = lab
        self.root_folder = root_folder
        self.api_key = api_key
        self.history: list[dict[str, Any]] = []
        self.exclusions: list[dict[str, Any]] = []
        self.commands: dict[int, dict[str, Any]] = {}
        self.ids = {"history": 0, "command": 0, "exclusion": 0, "file": 0, "item": 0, "episode": 0}
        a = self.api
        self.route("GET", a + "/system/status", lambda r: {"appName": self.app, "version": self.version})
        self.route("GET", a + "/rootfolder", lambda r: [{"id": 1, "path": self.root_folder, "accessible": True}])
        self.route("GET", a + "/qualityprofile", lambda r: [{"id": 4, "name": "HD-1080p"}, {"id": 6, "name": "Ultra-HD"}])
        self.route("GET", a + "/history", self.get_history)
        self.route("POST", a + "/command", self.post_command)
        self.route("GET", a + r"/command/(?P<id>\d+)", lambda r: self.commands.get(int(r.params["id"])) or not_found("command"))

    def dispatch(self, req: Request) -> Response:
        if req.headers.get("x-api-key") != self.api_key:
            return Response(401, "Unauthorized")
        return super().dispatch(req)

    def next_id(self, kind: str) -> int:
        self.ids[kind] += 1
        return self.ids[kind]

    # ------------------------------------------------------------------ history
    def add_history(self, event: str, download_id: str | None, source: str, data: dict[str, Any], **ids: Any) -> dict[str, Any]:
        rec = {"id": self.next_id("history"), "eventType": event, "date": self.lab.clock(), "sourceTitle": source,
               "data": data, **ids}
        if download_id:
            rec["downloadId"] = download_id.upper()  # the arrs keep qBittorrent hashes in upper case
        self.history.append(rec)
        return rec

    def get_history(self, r: Request) -> dict[str, Any]:
        recs = sorted(self.history, key=lambda h: (h["date"], h["id"]), reverse=True)
        if r.query.get("eventType"):
            recs = [h for h in recs if h["eventType"] == EVENT_TYPES[int(r.query["eventType"])]]
        page, size = int(r.query.get("page", 1)), int(r.query.get("pageSize", 10))
        return {"page": page, "pageSize": size, "totalRecords": len(recs), "records": recs[(page - 1) * size: page * size]}

    # ------------------------------------------------------------------ commands
    def post_command(self, r: Request) -> dict[str, Any]:
        body = r.json()
        cmd = {"id": self.next_id("command"), "name": body["name"], "body": body, "status": "completed",
               "result": "successful"}
        self.commands[cmd["id"]] = cmd
        if body["name"] in ("DownloadedMoviesScan", "DownloadedEpisodesScan"):
            self.import_download(self.lab.local(body["path"]), (body.get("downloadClientId") or "").lower())
        return cmd

    def import_download(self, path: str, download_id: str) -> None:
        raise NotImplementedError

    @staticmethod
    def videos_under(path: str) -> list[str]:
        if os.path.isfile(path):
            return [path] if path.lower().endswith(VIDEO) else []
        return sorted(os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs
                      if f.lower().endswith(VIDEO) and "sample" not in f.lower())


class FakeRadarr(FakeServarr):
    app = "Radarr"
    version = "5.26.2.10099"

    def __init__(self, lab: Lab, root_folder: str, api_key: str) -> None:
        super().__init__(lab, root_folder, api_key)
        self.movies: dict[int, dict[str, Any]] = {}
        a = self.api
        self.route("GET", a + "/movie", self.list_movies)
        self.route("POST", a + "/movie", self.add_movie)
        self.route("GET", a + r"/movie/lookup/tmdb", self.lookup_tmdb)
        self.route("GET", a + r"/movie/lookup", self.lookup_term)
        self.route("GET", a + r"/movie/(?P<id>\d+)", lambda r: self.movies.get(int(r.params["id"])) or not_found("movie"))
        self.route("PUT", a + r"/movie/(?P<id>\d+)", self.put_movie)
        self.route("DELETE", a + r"/movie/(?P<id>\d+)", self.delete_movie)
        self.route("GET", a + "/history/movie", lambda r: [h for h in self.history if h.get("movieId") == int(r.query["movieId"])])
        self.route("GET", a + "/exclusions", lambda r: self.exclusions)
        self.route("DELETE", a + r"/exclusions/(?P<id>\d+)", self.delete_exclusion)
        self.route("GET", a + "/parse", self.parse)

    # ------------------------------------------------------------------ state helpers (used by the lab)
    def create_movie(self, tmdb: int, title: str, year: int, path: str, *, monitored: bool = True,
                     profile: int = 4) -> dict[str, Any]:
        info = self.lab.movie_catalog[tmdb]
        m = {"id": self.next_id("item"), "title": title, "year": year, "tmdbId": tmdb, "imdbId": info["imdb"], "path": path,
             "hasFile": False, "monitored": monitored, "qualityProfileId": profile, "minimumAvailability": "released",
             "tags": [], "folderName": path}
        self.movies[m["id"]] = m
        return m

    def set_movie_file(self, m: dict[str, Any], path: str) -> None:
        """path: the file on disk (stored as the app sees it)."""
        m["hasFile"] = True
        m["movieFile"] = {"id": self.next_id("file"), "movieId": m["id"], "path": self.lab.app(path), "size": os.path.getsize(path),
                          "relativePath": os.path.basename(path)}

    def movie_for_path(self, path: str) -> dict[str, Any] | None:
        return next((m for m in self.movies.values() if path.startswith(m["path"].rstrip("/") + "/")), None)

    # ------------------------------------------------------------------ routes
    def list_movies(self, r: Request) -> list[dict[str, Any]]:
        out = list(self.movies.values())
        if r.query.get("tmdbId"):
            out = [m for m in out if m["tmdbId"] == int(r.query["tmdbId"])]
        return out

    def lookup_tmdb(self, r: Request) -> dict[str, Any] | Response:
        tmdb = int(r.query["tmdbId"])
        info = self.lab.movie_catalog.get(tmdb)
        if not info:
            return not_found("movie")
        return {"title": info["title"], "year": info["year"], "tmdbId": tmdb, "imdbId": info["imdb"], "qualityProfileId": 0,
                "monitored": False, "minimumAvailability": "announced", "tags": [], "images": []}

    def lookup_term(self, r: Request) -> list[dict[str, Any]]:
        term = r.query.get("term", "").casefold()
        return [{"title": i["title"], "year": i["year"], "tmdbId": t, "imdbId": i["imdb"], "qualityProfileId": 0}
                for t, i in self.lab.movie_catalog.items() if i["title"].casefold() in term]

    def add_movie(self, r: Request) -> dict[str, Any] | Response:
        b = r.json()
        if any(m["tmdbId"] == b["tmdbId"] for m in self.movies.values()):
            return Response(400, [{"errorMessage": "This movie has already been added"}])
        info = self.lab.movie_catalog[b["tmdbId"]]
        path = b.get("path") or os.path.join(b["rootFolderPath"], f"{info['title']} ({info['year']})")
        m = self.create_movie(b["tmdbId"], b.get("title") or info["title"], b.get("year") or info["year"], path,
                              monitored=b.get("monitored", True), profile=b["qualityProfileId"])
        m["minimumAvailability"] = b.get("minimumAvailability") or "released"
        m["tags"] = b.get("tags") or []
        m["addOptions"] = b.get("addOptions")
        return m

    def put_movie(self, r: Request) -> dict[str, Any] | Response:
        m = self.movies.get(int(r.params["id"]))
        if not m:
            return not_found("movie")
        b = r.json()
        m.update({k: b[k] for k in ("monitored", "qualityProfileId", "minimumAvailability", "tags") if k in b})
        return m

    def delete_movie(self, r: Request) -> Response | None:
        m = self.movies.pop(int(r.params["id"]), None)
        if not m:
            return not_found("movie")
        if r.flag("deleteFiles"):
            shutil.rmtree(self.lab.local(m["path"]), ignore_errors=True)
        if r.flag("addImportExclusion"):
            self.exclusions.append({"id": self.next_id("exclusion"), "tmdbId": m["tmdbId"], "movieTitle": m["title"],
                                    "movieYear": m["year"]})
        self.history = [h for h in self.history if h.get("movieId") != m["id"]]  # Radarr drops a deleted movie's history
        return None

    def delete_exclusion(self, r: Request) -> None:
        self.exclusions = [e for e in self.exclusions if e["id"] != int(r.params["id"])]

    def parse(self, r: Request) -> dict[str, Any]:
        title = r.query.get("title", "")
        m = re.match(r"(.+?)[. ]\(?(\d{4})\)?[. ]", title)
        out: dict[str, Any] = {"title": title}
        if m:
            name = m.group(1).replace(".", " ")
            out["parsedMovieInfo"] = {"movieTitles": [name], "year": int(m.group(2))}
            hit = next((x for x in self.movies.values() if x["title"].casefold() == name.casefold()), None)
            if hit:
                out["movie"] = hit
        return out

    # ------------------------------------------------------------------ imports (Undo, Redownload)
    def import_download(self, path: str, download_id: str) -> None:
        release = self.lab.releases.get(download_id) or {}
        m = next((x for x in self.movies.values() if x["tmdbId"] == release.get("tmdb")), None)
        files = self.videos_under(path)
        if not m or not files:
            return
        src = files[0]
        dest = os.path.join(self.lab.local(m["path"]), os.path.basename(src))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.exists(dest):
            os.link(src, dest)  # importMode Copy hardlinks when it can
        self.set_movie_file(m, dest)
        self.add_history("downloadFolderImported", download_id, release.get("name", ""),
                         {"importedPath": self.lab.app(dest), "droppedPath": self.lab.app(src)}, movieId=m["id"])


class FakeSonarr(FakeServarr):
    app = "Sonarr"
    version = "4.0.15.2941"

    def __init__(self, lab: Lab, root_folder: str, api_key: str) -> None:
        super().__init__(lab, root_folder, api_key)
        self.series: dict[int, dict[str, Any]] = {}
        self.episodes: dict[int, dict[str, Any]] = {}
        self.files: dict[int, dict[str, Any]] = {}
        a = self.api
        self.route("GET", a + "/series", self.list_series)
        self.route("POST", a + "/series", self.add_series)
        self.route("GET", a + "/series/lookup", self.lookup)
        self.route("GET", a + r"/series/(?P<id>\d+)", lambda r: self.full_series(int(r.params["id"])) or not_found("series"))
        self.route("PUT", a + r"/series/(?P<id>\d+)", self.put_series)
        self.route("DELETE", a + r"/series/(?P<id>\d+)", self.delete_series)
        self.route("GET", a + "/episode", lambda r: self.series_episodes(int(r.query["seriesId"])))
        self.route("PUT", a + "/episode/monitor", self.monitor_episodes)
        self.route("GET", a + "/episodefile", lambda r: [f for f in self.files.values() if f["seriesId"] == int(r.query["seriesId"])])
        self.route("DELETE", a + "/episodefile/bulk", self.delete_files)
        self.route("GET", a + "/history/series", lambda r: [h for h in self.history if h.get("seriesId") == int(r.query["seriesId"])])
        self.route("GET", a + "/importlistexclusion", lambda r: self.exclusions)
        self.route("DELETE", a + r"/importlistexclusion/(?P<id>\d+)", self.delete_exclusion)
        self.route("GET", a + "/parse", self.parse)

    # ------------------------------------------------------------------ state helpers (used by the lab)
    def create_series(self, tvdb: int, path: str, *, monitored: bool = True, season_folder: bool = True,
                      monitor_episodes: bool = True, profile: int = 4, seasons: dict[int, bool] | None = None) -> dict[str, Any]:
        info = self.lab.series_catalog[tvdb]
        s = {"id": self.next_id("item"), "title": info["title"], "year": info["year"], "tvdbId": tvdb, "tmdbId": info["tmdb"],
             "imdbId": info["imdb"], "path": path, "seasonFolder": season_folder, "seriesType": "standard",
             "monitored": monitored, "monitorNewItems": "all", "qualityProfileId": profile, "tags": [],
             "seasons": [{"seasonNumber": n, "monitored": (seasons or {}).get(n, monitored)} for n in sorted(info["seasons"])]}
        self.series[s["id"]] = s
        for sn, eps in sorted(info["seasons"].items()):
            for ep in eps:
                eid = self.next_id("episode")
                self.episodes[eid] = {"id": eid, "seriesId": s["id"], "seasonNumber": sn, "episodeNumber": ep["number"],
                                      "title": ep["title"], "airDateUtc": ep["air"], "monitored": monitor_episodes,
                                      "hasFile": False, "episodeFileId": 0, "tvdbId": ep["tvdb"]}
        return s

    def episode(self, series_id: int, season: int, number: int) -> dict[str, Any]:
        return next(e for e in self.episodes.values()
                    if e["seriesId"] == series_id and e["seasonNumber"] == season and e["episodeNumber"] == number)

    def set_episode_file(self, s: dict[str, Any], eps: list[dict[str, Any]], path: str) -> dict[str, Any]:
        """path: the file on disk (stored as the app sees it)."""
        for e in eps:  # an upgrade replaces the old file record
            if e["episodeFileId"]:
                self.files.pop(e["episodeFileId"], None)
        f = {"id": self.next_id("file"), "seriesId": s["id"], "seasonNumber": eps[0]["seasonNumber"], "path": self.lab.app(path),
             "size": os.path.getsize(path), "relativePath": os.path.relpath(self.lab.app(path), s["path"])}
        self.files[f["id"]] = f
        for e in eps:
            e.update(hasFile=True, episodeFileId=f["id"])
        return f

    def series_episodes(self, sid: int) -> list[dict[str, Any]]:
        return sorted((e for e in self.episodes.values() if e["seriesId"] == sid), key=lambda e: (e["seasonNumber"], e["episodeNumber"]))

    def full_series(self, sid: int) -> dict[str, Any] | None:
        s = self.series.get(sid)
        if s:
            s["statistics"] = {"episodeFileCount": sum(1 for f in self.files.values() if f["seriesId"] == sid)}
        return s

    # ------------------------------------------------------------------ routes
    def list_series(self, r: Request) -> list[dict[str, Any]]:
        return [self.full_series(sid) for sid in list(self.series)]  # type: ignore[misc]

    def lookup(self, r: Request) -> list[dict[str, Any]]:
        term = r.query.get("term", "")
        hits = []
        for tvdb, i in self.lab.series_catalog.items():
            if term == f"tvdb:{tvdb}" or i["title"].casefold() in term.casefold():
                hits.append({"title": i["title"], "year": i["year"], "tvdbId": tvdb, "tmdbId": i["tmdb"], "imdbId": i["imdb"],
                             "qualityProfileId": 0, "seasonFolder": True, "monitored": False,
                             "seasons": [{"seasonNumber": n, "monitored": False} for n in sorted(i["seasons"])]})
        return hits

    def add_series(self, r: Request) -> dict[str, Any] | Response:
        b = r.json()
        if any(s["tvdbId"] == b["tvdbId"] for s in self.series.values()):
            return Response(400, [{"errorMessage": "This series has already been added"}])
        info = self.lab.series_catalog[b["tvdbId"]]
        path = b.get("path") or os.path.join(b["rootFolderPath"], info["title"])
        monitor_none = (b.get("addOptions") or {}).get("monitor") == "none"
        s = self.create_series(b["tvdbId"], path, monitored=b.get("monitored", True), season_folder=b.get("seasonFolder", True),
                               monitor_episodes=not monitor_none, profile=b["qualityProfileId"],
                               seasons={x["seasonNumber"]: bool(x.get("monitored")) and not monitor_none for x in b.get("seasons", [])})
        s["addOptions"] = b.get("addOptions")
        return s

    def put_series(self, r: Request) -> dict[str, Any] | Response:
        s = self.series.get(int(r.params["id"]))
        if not s:
            return not_found("series")
        b = r.json()
        s["monitored"] = b.get("monitored", s["monitored"])
        want = {x["seasonNumber"]: x["monitored"] for x in b.get("seasons", [])}
        for x in s["seasons"]:
            x["monitored"] = want.get(x["seasonNumber"], x["monitored"])
        return s

    def delete_series(self, r: Request) -> Response | None:
        s = self.series.pop(int(r.params["id"]), None)
        if not s:
            return not_found("series")
        if r.flag("deleteFiles"):
            shutil.rmtree(self.lab.local(s["path"]), ignore_errors=True)
        if r.flag("addImportListExclusion"):
            self.exclusions.append({"id": self.next_id("exclusion"), "tvdbId": s["tvdbId"], "title": s["title"]})
        self.episodes = {k: e for k, e in self.episodes.items() if e["seriesId"] != s["id"]}
        self.files = {k: f for k, f in self.files.items() if f["seriesId"] != s["id"]}
        self.history = [h for h in self.history if h.get("seriesId") != s["id"]]  # Sonarr drops a deleted show's history
        return None

    def monitor_episodes(self, r: Request) -> None:
        b = r.json()
        for i in b["episodeIds"]:
            if i in self.episodes:
                self.episodes[i]["monitored"] = b["monitored"]

    def delete_files(self, r: Request) -> None:
        for fid in r.json()["episodeFileIds"]:
            f = self.files.pop(fid, None)
            if not f:
                continue
            p = self.lab.local(f["path"])
            if os.path.exists(p):
                os.remove(p)  # Sonarr deletes the file itself too
            for e in self.episodes.values():
                if e["episodeFileId"] == fid:
                    e.update(hasFile=False, episodeFileId=0)
                    self.add_history("episodeFileDeleted", None, os.path.basename(f["path"]), {"reason": "Manual"},
                                     seriesId=f["seriesId"], episodeId=e["id"])

    def delete_exclusion(self, r: Request) -> None:
        self.exclusions = [e for e in self.exclusions if e["id"] != int(r.params["id"])]

    def parse(self, r: Request) -> dict[str, Any]:
        title = r.query.get("title", "")
        out: dict[str, Any] = {"title": title}
        m = EPISODE_RE.search(title) or re.search(r"S(\d+)(?![E\d])", title, re.I)
        if not m:
            return out
        name = title[: m.start()].strip(". -").replace(".", " ")
        season = int(m.group(1))
        numbers = list(range(int(m.group(2)), int(m.group(3) or m.group(2)) + 1)) if m.re is EPISODE_RE else []
        out["parsedEpisodeInfo"] = {"seriesTitle": name, "seasonNumber": season, "episodeNumbers": numbers,
                                    "fullSeason": not numbers}
        s = next((x for x in self.series.values() if x["title"].casefold() == name.casefold()), None)
        if s:
            out["series"] = s
            out["episodes"] = [e for e in self.series_episodes(s["id"]) if e["seasonNumber"] == season and e["episodeNumber"] in numbers]
        return out

    # ------------------------------------------------------------------ imports (Undo, Redownload)
    def import_download(self, path: str, download_id: str) -> None:
        release = self.lab.releases.get(download_id) or {}
        s = next((x for x in self.series.values() if x["tvdbId"] == release.get("tvdb")), None)
        if not s:
            return
        for src in self.videos_under(path):
            m = EPISODE_RE.search(os.path.basename(src))
            if not m:
                continue
            season, first, last = int(m.group(1)), int(m.group(2)), int(m.group(3) or m.group(2))
            eps = [self.episode(s["id"], season, n) for n in range(first, last + 1)]
            folder = os.path.join(self.lab.local(s["path"]), f"Season {season:02d}") if s["seasonFolder"] else self.lab.local(s["path"])
            dest = os.path.join(folder, os.path.basename(src))
            os.makedirs(folder, exist_ok=True)
            if not os.path.exists(dest):
                os.link(src, dest)
            self.set_episode_file(s, eps, dest)
            for e in eps:
                self.add_history("downloadFolderImported", download_id, release.get("name", ""),
                                 {"importedPath": self.lab.app(dest), "droppedPath": self.lab.app(src)},
                                 seriesId=s["id"], episodeId=e["id"])


class FakeProwlarr(FakeServer):
    name = "prowlarr"

    def __init__(self) -> None:
        super().__init__()
        self.indexers = [
            {"id": 1, "name": "ExampleTracker", "protocol": "torrent", "indexerUrls": ["https://www.example.org/"],
             "fields": [{"name": "baseUrl", "value": "https://example.org"}]},
            {"id": 2, "name": "UsenetThing", "protocol": "usenet", "indexerUrls": ["https://nzb.example.com/"], "fields": []},
            {"id": 3, "name": "NewSite", "protocol": "torrent", "indexerUrls": ["https://newsite.example.net/"], "fields": []},
        ]
        self.route("GET", "/api/v1/system/status", lambda r: {"appName": "Prowlarr", "version": "2.0.5.5160"})
        self.route("GET", "/api/v1/indexer", lambda r: self.indexers)
