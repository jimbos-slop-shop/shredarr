"""Fake qBittorrent, Transmission and Deluge, all showing the same torrents (the lab's) in their own API shapes.
Removing a torrent with its data really deletes the files."""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .server import FakeServer, Request, Response

if TYPE_CHECKING:
    from .lab import Lab


@dataclass
class Torrent:
    hash: str
    name: str
    save_path: str  # as the torrent client sees it
    content_path: str  # as the torrent client sees it
    disk_path: str  # content_path on the lab's disk
    category: str
    tracker: str
    added_on: int
    ratio: float = 0.0
    seeding_time: int = 0
    progress: float = 1.0
    upspeed: int = 0
    dlspeed: int = 0
    size_override: int | None = None

    @property
    def size(self) -> int:
        if self.size_override is not None:
            return self.size_override
        p = self.disk_path
        if os.path.isfile(p):
            return os.path.getsize(p)
        return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(p) for f in fs)

    @property
    def state(self) -> str:
        if self.progress < 1:
            return "downloading"
        return "uploading" if self.upspeed else "stalledUP"

    def export(self) -> bytes:
        """Stand-in for a .torrent file: bencoded, and carrying the hash so a re-add can be recognized."""
        name = self.name.encode()
        return b"d7:comment40:" + self.hash.encode() + b"4:infod4:name" + str(len(name)).encode() + b":" + name + b"ee"


def remove_data(t: Torrent) -> None:
    if os.path.isdir(t.disk_path):
        shutil.rmtree(t.disk_path, ignore_errors=True)
    elif os.path.exists(t.disk_path):
        os.remove(t.disk_path)


class TorrentFake(FakeServer):
    def __init__(self, lab: Lab) -> None:
        super().__init__()
        self.lab = lab

    def remove(self, hashes: list[str], with_data: bool) -> None:
        for h in hashes:
            self.lab.remove_torrent(h.lower(), with_data)

    def readd(self, content: bytes, category: str) -> Torrent | None:
        """Re-adding a saved .torrent brings back the torrent the lab knows by that hash (downloads it again)."""
        m = re.search(rb"comment40:([0-9a-f]{40})", content)
        if not m:
            return None
        return self.lab.redownload(m.group(1).decode(), category)


class FakeQBittorrent(TorrentFake):
    name = "qbittorrent"
    username, password = "admin", "adminadmin"

    def __init__(self, lab: Lab) -> None:
        super().__init__(lab)
        self.sessions: set[str] = set()
        a = "/api/v2"
        self.route("POST", a + "/auth/login", self.login)
        for method in ("GET", "POST"):
            self.route(method, a + "/app/version", self.guard(lambda r: "v5.1.2"))
            self.route(method, a + "/app/webapiVersion", self.guard(lambda r: "2.11.4"))
            self.route(method, a + "/torrents/info", self.guard(self.info))
            self.route(method, a + "/torrents/export", self.guard(self.export))
        self.route("POST", a + "/torrents/delete", self.guard(self.delete))
        self.route("POST", a + "/torrents/add", self.guard(self.add))

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def guard(self, fn: Any) -> Any:
        def wrapped(r: Request) -> Any:
            sid = re.search(r"SID=([^;]+)", r.headers.get("cookie", ""))
            if not sid or sid.group(1) not in self.sessions:
                return Response(403, "Forbidden")
            return fn(r)
        return wrapped

    def login(self, r: Request) -> Response:
        f = r.form()
        if f.get("username") != self.username or f.get("password") != self.password:
            return Response(200, "Fails.")
        sid = secrets.token_hex(8)
        self.sessions.add(sid)
        return Response(200, "Ok.", {"Set-Cookie": f"SID={sid}; HttpOnly; path=/"})

    def params(self, r: Request) -> dict[str, str]:
        return {**r.query, **(r.form() if r.body and b"=" in r.body and b"\r\n" not in r.body else {})}

    def info(self, r: Request) -> list[dict[str, Any]]:
        want = {h.lower() for h in self.params(r).get("hashes", "").split("|") if h}
        return [{"hash": t.hash, "name": t.name, "save_path": t.save_path, "content_path": t.content_path, "size": t.size,
                 "total_size": t.size, "progress": t.progress, "ratio": t.ratio, "seeding_time": t.seeding_time, "state": t.state,
                 "upspeed": t.upspeed, "dlspeed": t.dlspeed, "added_on": t.added_on, "category": t.category, "tracker": t.tracker,
                 "tags": ""}
                for t in self.lab.torrents.values() if not want or t.hash in want]

    def export(self, r: Request) -> Response:
        t = self.lab.torrents.get(self.params(r).get("hash", "").lower())
        if not t:
            return Response(404, "Not Found")
        return Response(200, t.export(), {"Content-Type": "application/x-bittorrent"})

    def delete(self, r: Request) -> None:
        f = r.form()
        self.remove(f["hashes"].split("|"), f.get("deleteFiles", "").lower() == "true")  # qBittorrent ignores case here

    def add(self, r: Request) -> str:
        cat = re.search(rb'name="category"\r\n\r\n(.*?)\r\n', r.body)
        self.readd(r.body, cat.group(1).decode() if cat else "")
        return "Ok."


TR_STATUS = {"downloading": 4, "uploading": 6, "stalledUP": 6}


class FakeTransmission(TorrentFake):
    name = "transmission"
    username, password = "", ""

    def __init__(self, lab: Lab) -> None:
        super().__init__(lab)
        self.session_id = secrets.token_hex(8)
        self.route("POST", "/transmission/rpc", self.rpc)

    def fields(self, t: Torrent, tid: int) -> dict[str, Any]:
        return {"id": tid, "hashString": t.hash, "name": t.name, "downloadDir": t.save_path, "sizeWhenDone": t.size,
                "totalSize": t.size, "percentDone": t.progress, "uploadRatio": t.ratio, "secondsSeeding": t.seeding_time,
                "status": TR_STATUS[t.state], "error": 0, "errorString": "", "rateUpload": t.upspeed, "rateDownload": t.dlspeed,
                "addedDate": t.added_on, "labels": [t.category] if t.category else [],
                "trackers": [{"announce": t.tracker, "id": 0, "tier": 0}], "torrentFile": f"/config/torrents/{t.hash}.torrent"}

    def rpc(self, r: Request) -> Response:
        if self.username or self.password:
            want = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            if r.headers.get("authorization") != want:
                return Response(401, "Unauthorized")
        if r.headers.get("x-transmission-session-id") != self.session_id:
            return Response(409, "Conflict", {"X-Transmission-Session-Id": self.session_id})
        body = r.json()
        method, args = body.get("method"), body.get("arguments") or {}
        ids = list(self.lab.torrents)
        if method == "session-get":
            out: dict[str, Any] = {"version": "4.0.6 (38c164933e)", "rpc-version": 18, "rpc-version-minimum": 14,
                                   "rpc-version-semver": "5.4.0", "download-dir": self.lab.downloads}
        elif method == "torrent-get":
            sel = args.get("ids")
            ts = [(i + 1, self.lab.torrents[h]) for i, h in enumerate(ids)
                  if sel is None or h in [str(x).lower() for x in sel] or (i + 1) in sel]
            fields = args.get("fields")
            out = {"torrents": [{k: v for k, v in self.fields(t, i).items() if not fields or k in fields or k == "id"} for i, t in ts]}
        elif method == "torrent-remove":
            hashes = [h for i, h in enumerate(ids) if h in [str(x).lower() for x in args["ids"]] or (i + 1) in args["ids"]]
            self.remove(hashes, bool(args.get("delete-local-data")))
            out = {}
        elif method == "torrent-add":
            t = self.readd(base64.b64decode(args["metainfo"]), (args.get("labels") or [""])[0])
            out = {"torrent-added": {"id": len(self.lab.torrents), "name": t.name, "hashString": t.hash}} if t else {}
        else:
            return Response(200, {"result": f"method name not recognized: {method}", "arguments": {}, "tag": body.get("tag")})
        return Response(200, {"result": "success", "arguments": out, "tag": body.get("tag")})


DELUGE_STATE = {"downloading": "Downloading", "uploading": "Seeding", "stalledUP": "Seeding"}


class FakeDeluge(TorrentFake):
    name = "deluge"
    password = "deluge"

    def __init__(self, lab: Lab) -> None:
        super().__init__(lab)
        self.connected = False
        self.labels: list[str] = []
        self.torrent_labels: dict[str, str] = {}
        self.sessions: set[str] = set()
        self.route("POST", "/json", self.rpc)

    def status(self, t: Torrent) -> dict[str, Any]:
        return {"name": t.name, "save_path": t.save_path, "download_location": t.save_path, "total_wanted": t.size,
                "progress": t.progress * 100, "ratio": t.ratio, "seeding_time": t.seeding_time, "state": DELUGE_STATE[t.state],
                "upload_payload_rate": t.upspeed, "download_payload_rate": t.dlspeed, "time_added": float(t.added_on),
                "label": self.torrent_labels.get(t.hash, t.category), "trackers": [{"url": t.tracker, "tier": 0}]}

    def rpc(self, r: Request) -> Response:
        body = r.json()
        method, params, rid = body["method"], body.get("params") or [], body.get("id")

        def ok(result: Any, headers: dict[str, str] | None = None) -> Response:
            return Response(200, {"result": result, "error": None, "id": rid}, headers or {})

        if method == "auth.login":
            if params[:1] != [self.password]:
                return ok(False)
            sid = secrets.token_hex(8)
            self.sessions.add(sid)
            return ok(True, {"Set-Cookie": f"_session_id={sid}; Path=/json"})
        sid = re.search(r"_session_id=([^;]+)", r.headers.get("cookie", ""))
        if not sid or sid.group(1) not in self.sessions:
            return Response(200, {"result": None, "error": {"message": "Not authenticated", "code": 1}, "id": rid})
        if method == "web.connected":
            return ok(self.connected)
        if method == "web.get_hosts":
            return ok([["c0ffee", "127.0.0.1", 58846, "localclient"]])
        if method == "web.connect":
            self.connected = True
            return ok([])
        if method == "daemon.get_version":
            return ok("2.1.1")
        if method == "core.get_torrents_status":
            want = {h.lower() for h in (params[0] or {}).get("id", [])}
            return ok({t.hash: self.status(t) for t in self.lab.torrents.values() if not want or t.hash in want})
        if method == "core.remove_torrent":
            self.remove([params[0]], bool(params[1]))
            return ok(True)
        if method == "core.add_torrent_file":
            t = self.readd(base64.b64decode(params[1]), "")
            return ok(t.hash if t else None)
        if method == "label.get_labels":
            return ok(self.labels)
        if method == "label.add":
            self.labels.append(params[0])
            return ok(None)
        if method == "label.set_torrent":
            self.torrent_labels[params[0]] = params[1]
            t = self.lab.torrents.get(params[0])
            if t:
                t.category = params[1]
            return ok(None)
        return Response(200, {"result": None, "error": {"message": f"Unknown method {method}", "code": 2}, "id": rid})
