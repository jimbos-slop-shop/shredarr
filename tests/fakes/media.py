"""Fake Jellyfin, Plex, Seerr and a notification endpoint (ntfy)."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import quoteattr

from .server import FakeServer, Request, Response

if TYPE_CHECKING:
    from .lab import Lab

VIDEO = (".mkv", ".mp4", ".avi")


def item_id(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()


def dashed(i: str) -> str:
    """Jellyfin writes ids with dashes in webhooks."""
    return f"{i[:8]}-{i[8:12]}-{i[12:16]}-{i[16:20]}-{i[20:]}"


class FakeJellyfin(FakeServer):
    name = "jellyfin"
    api_key = "jellyfin-key"
    plugin_id = "71552a5a5c5c4350a2aeebe451a30173"

    def __init__(self, lab: Lab) -> None:
        super().__init__()
        self.lab = lab
        self.prefix: tuple[str, str] | None = None  # (lab path, Jellyfin path) when Jellyfin mounts the library elsewhere
        self.plugin_config: dict[str, Any] = {"ServerUrl": "", "GenericOptions": []}
        self.webhook_target: tuple[str, str] | None = None  # (url, token): send ItemDeleted webhooks there
        self.route("GET", "/System/Info", self.auth(lambda r: {"Version": "10.11.0", "ServerName": "Lab"}))
        self.route("GET", "/Items", self.auth(self.items))
        self.route("DELETE", r"/Items/(?P<id>[0-9a-f]{32})", self.auth(self.delete))
        self.route("GET", "/Library/VirtualFolders", self.auth(lambda r: [
            {"Name": "Movies", "CollectionType": "movies", "Locations": [self.jf_path(self.lab.movies)]},
            {"Name": "Shows", "CollectionType": "tvshows", "Locations": [self.jf_path(self.lab.tv)]}]))
        self.route("POST", "/Library/Media/Updated", self.auth(lambda r: None))
        self.route("POST", "/Library/Refresh", self.auth(lambda r: None))
        self.route("GET", "/Plugins", self.auth(lambda r: [{"Name": "Webhook", "Id": self.plugin_id, "Version": "18.0.0.0"}]))
        self.route("GET", rf"/Plugins/{self.plugin_id}/Configuration", self.auth(lambda r: self.plugin_config))
        self.route("POST", rf"/Plugins/{self.plugin_id}/Configuration", self.auth(self.save_plugin))

    def auth(self, fn: Any) -> Any:
        def wrapped(r: Request) -> Any:
            if r.headers.get("x-emby-token") != self.api_key:
                return Response(401, "Unauthorized")
            return fn(r)
        return wrapped

    def jf_path(self, p: str) -> str:
        if self.prefix and p.startswith(self.prefix[0]):
            return self.prefix[1] + p[len(self.prefix[0]):]
        return p

    def save_plugin(self, r: Request) -> None:
        self.plugin_config = r.json()

    # ------------------------------------------------------------------ items, built from what's on disk
    def all_items(self) -> list[dict[str, Any]]:
        out = []
        for root, kind in ((self.lab.movies, "Movie"), (self.lab.tv, "Episode")):
            for d, _, fs in os.walk(root):
                for f in sorted(fs):
                    p = os.path.join(d, f)
                    if f.lower().endswith(VIDEO):
                        out.append(self.describe(p, kind))
        for s in self.lab.series_refs.values():
            if os.path.isdir(s.path):
                out.append({"Id": s.jellyfin_id, "Type": "Series", "Name": s.title, "Path": self.jf_path(s.path),
                            "ProviderIds": {"Tvdb": str(s.tvdb), "Tmdb": str(s.tmdb), "Imdb": s.imdb}})
        return out

    def describe(self, path: str, kind: str) -> dict[str, Any]:
        it: dict[str, Any] = {"Id": item_id(path), "Type": kind, "Path": self.jf_path(path), "Name": os.path.splitext(os.path.basename(path))[0]}
        if kind == "Movie":
            m = self.lab.movie_by_folder(os.path.dirname(path))
            if m:
                it.update(Name=m.title, ProductionYear=m.year, ProviderIds={"Tmdb": str(m.tmdb), "Imdb": m.imdb})
        else:
            s = self.lab.series_by_path(path)
            ep = re.search(r"S(\d+)E(\d+)(?:-?E(\d+))?", os.path.basename(path), re.I)
            if s:
                it.update(SeriesName=s.title, SeriesId=s.jellyfin_id)
            if ep:
                it.update(ParentIndexNumber=int(ep.group(1)), IndexNumber=int(ep.group(2)),
                          IndexNumberEnd=int(ep.group(3)) if ep.group(3) else None)
        return it

    def items(self, r: Request) -> dict[str, Any]:
        items = self.all_items()
        if r.query.get("ids"):
            want = set(r.query["ids"].replace("-", "").split(","))
            items = [i for i in items if i["Id"] in want]
        if r.query.get("includeItemTypes"):
            types = set(r.query["includeItemTypes"].split(","))
            items = [i for i in items if i["Type"] in types]
        return {"Items": items, "TotalRecordCount": len(items)}

    def delete(self, r: Request) -> Response | None:
        it = next((i for i in self.all_items() if i["Id"] == r.params["id"]), None)
        if not it:
            return Response(404, "Not Found")
        p = self.lab.local_from_jellyfin(it["Path"])
        if os.path.isfile(p):
            os.remove(p)
        if self.webhook_target:
            threading.Thread(target=self.send_webhook, args=(it,), daemon=True).start()
        return None

    def send_webhook(self, it: dict[str, Any]) -> None:
        """Jellyfin tells the Webhook plugin about deletes a little later (it batches them)."""
        time.sleep(0.3)
        assert self.webhook_target
        url, token = self.webhook_target
        payload = self.lab.event_for_item(it)
        req = urllib.request.Request(url, data=__import__("json").dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "X-Webhook-Token": token})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass


class FakePlex(FakeServer):
    name = "plex"
    token = "plex-token"

    def __init__(self, lab: Lab) -> None:
        super().__init__()
        self.lab = lab
        self.route("GET", "/", self.auth(self.root))
        self.route("GET", "/library", self.auth(self.library))
        self.route("GET", "/library/sections", self.auth(self.sections))
        self.route("GET", r"/library/sections/(?P<key>\d+)/refresh", self.auth(lambda r: None))

    def auth(self, fn: Any) -> Any:
        def wrapped(r: Request) -> Any:
            if (r.headers.get("x-plex-token") or r.query.get("X-Plex-Token")) != self.token:
                return Response(401, "Unauthorized")
            return fn(r)
        return wrapped

    @staticmethod
    def wants_json(r: Request) -> bool:
        return "application/json" in r.headers.get("accept", "")

    def root(self, r: Request) -> Response | dict[str, Any]:
        attrs = {"friendlyName": "Lab Plex", "machineIdentifier": "lab0001", "version": "1.42.1.10060-4e8b05daf",
                 "platform": "Linux", "myPlex": "0"}
        if self.wants_json(r):
            return {"MediaContainer": attrs}
        return self.xml("MediaContainer", attrs)

    def library(self, r: Request) -> Response:
        return self.xml("MediaContainer", {"size": "2", "title1": "Plex Library"},
                        '<Directory key="sections" title="Library Sections" />')

    def section_list(self) -> list[dict[str, Any]]:
        return [{"key": "1", "type": "movie", "title": "Movies", "agent": "tv.plex.agents.movie", "uuid": "m1",
                 "Location": [{"id": 1, "path": self.lab.movies}]},
                {"key": "2", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series", "uuid": "s2",
                 "Location": [{"id": 2, "path": self.lab.tv}]}]

    def sections(self, r: Request) -> Response | dict[str, Any]:
        secs = self.section_list()
        if self.wants_json(r):
            return {"MediaContainer": {"size": len(secs), "Directory": secs}}
        inner = "".join(
            "<Directory " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in s.items() if k != "Location") + ">"
            + "".join(f'<Location id="{loc["id"]}" path={quoteattr(loc["path"])} />' for loc in s["Location"]) + "</Directory>"
            for s in secs)
        return self.xml("MediaContainer", {"size": str(len(secs)), "title1": "Plex Library"}, inner)

    @staticmethod
    def xml(tag: str, attrs: dict[str, str], inner: str = "") -> Response:
        a = " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items())
        return Response(200, f'<?xml version="1.0" encoding="UTF-8"?>\n<{tag} {a}>{inner}</{tag}>', {"Content-Type": "text/xml"})

    def refreshes(self) -> list[tuple[str, str | None]]:
        out = []
        for r in self.requests("GET", r"/library/sections/\d+/refresh"):
            out.append((r.path.split("/")[3], r.query.get("path")))
        return out


class FakeSeerr(FakeServer):
    name = "seerr"
    api_key = "seerr-key"

    def __init__(self) -> None:
        super().__init__()
        self.media: dict[int, dict[str, Any]] = {}  # media id -> {"id", "tmdbId", "mediaType"}
        self.next_id = 100
        a = "/api/v1"
        self.route("GET", a + "/status", lambda r: {"version": "2.7.3", "commitTag": "v2.7.3"})
        self.route("GET", a + "/settings/main", self.auth(lambda r: {"applicationTitle": "Seerr"}))
        self.route("GET", a + r"/(?P<kind>movie|tv)/(?P<tmdb>\d+)", self.auth(self.details))
        self.route("DELETE", a + r"/media/(?P<id>\d+)", self.auth(self.delete))
        self.route("POST", a + r"/settings/jobs/(?P<job>[\w-]+)/run", self.auth(lambda r: {"id": r.params["job"]}))

    def auth(self, fn: Any) -> Any:
        def wrapped(r: Request) -> Any:
            if r.headers.get("x-api-key") != self.api_key:
                return Response(403, {"message": "You do not have permission to access this endpoint"})
            return fn(r)
        return wrapped

    def add(self, kind: str, tmdb: int) -> int:
        self.next_id += 1
        self.media[self.next_id] = {"id": self.next_id, "tmdbId": tmdb, "mediaType": kind, "status": 5}
        return self.next_id

    def details(self, r: Request) -> dict[str, Any]:
        kind, tmdb = r.params["kind"], int(r.params["tmdb"])
        info = next((m for m in self.media.values() if m["tmdbId"] == tmdb and m["mediaType"] == kind), None)
        return {"id": tmdb, **({"mediaInfo": info} if info else {})}

    def delete(self, r: Request) -> Response | None:
        if not self.media.pop(int(r.params["id"]), None):
            return Response(404, {"message": "Media not found"})
        return None


class FakeNtfy(FakeServer):
    name = "ntfy"

    def __init__(self) -> None:
        super().__init__()
        self.route("POST", "/", lambda r: {"id": "msg", "event": "message"})

    def messages(self) -> list[dict[str, Any]]:
        return [r.json() for r in self.requests("POST", "/")]

    def titles(self) -> list[str]:
        return [m["title"] for m in self.messages()]
