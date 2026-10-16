"""Deluge's Web UI JSON-RPC API (with the Label plugin for categories). Torrents come back in qBittorrent's shape.

A small client of its own: the deluge-web-client package pulls in a second HTTP stack (niquests, urllib3-future
and a compiled QUIC library) for these few calls.
"""

from __future__ import annotations

import base64
import itertools
import os
from collections.abc import Callable
from typing import Any

import httpx

from ...config import JSON
from ..base import TIMEOUT, ServiceError, ServiceHTTPError, ServiceUnreachable
from .base import Torrent

KEYS = [
    "name",
    "save_path",
    "download_location",
    "total_wanted",
    "progress",
    "ratio",
    "seeding_time",
    "state",
    "upload_payload_rate",
    "download_payload_rate",
    "time_added",
    "label",
    "trackers",
]


def state(t: JSON) -> str:
    done, st = t["progress"] >= 100, t.get("state") or ""
    return {
        "Seeding": "uploading" if t["upload_payload_rate"] else "stalledUP",
        "Downloading": "downloading",
        "Error": "error",
        "Checking": "checkingUP",
        "Moving": "moving",
        "Allocating": "allocating",
        "Paused": "pausedUP" if done else "pausedDL",
        "Queued": "queuedUP" if done else "queuedDL",
    }.get(st, st.lower())


class DelugeClient:
    def __init__(self, conf: JSON, local: Callable[[str], str] | None = None) -> None:
        url = conf["url"].rstrip("/")
        self.url = url if url.endswith("/json") else url + "/json"
        self._http = httpx.Client(timeout=TIMEOUT)
        self._ids = itertools.count(1)
        if not self.rpc("auth.login", conf.get("password", "")):
            raise ServiceError("wrong password")
        if not self.rpc("web.connected"):
            hosts = self.rpc("web.get_hosts")
            if not hosts:
                raise ServiceError("the Deluge web UI isn't connected to a Deluge daemon")
            self.rpc("web.connect", hosts[0][0])

    def rpc(self, method: str, *params: Any) -> Any:
        try:
            r = self._http.post(self.url, json={"method": method, "params": list(params), "id": next(self._ids)})
        except httpx.HTTPError as e:
            raise ServiceUnreachable(str(e) or type(e).__name__) from e
        if r.status_code >= 400:
            raise ServiceHTTPError(r.status_code, r.reason_phrase)
        out = r.json()
        if out.get("error"):
            raise ServiceError((out["error"] or {}).get("message") or "Deluge error")
        return out.get("result")

    def torrents(self, hashes: list[str] | None = None) -> list[Torrent]:
        res = self.rpc("core.get_torrents_status", {"id": list(hashes)} if hashes else {}, KEYS) or {}
        out: list[Torrent] = []
        for h, t in res.items():
            save = t.get("download_location") or t.get("save_path") or ""
            out.append(
                {
                    "hash": h.lower(),
                    "name": t["name"],
                    "save_path": save,
                    "content_path": os.path.join(save, t["name"]),
                    "size": t["total_wanted"],
                    "progress": t["progress"] / 100,
                    "ratio": max(0.0, t["ratio"]),
                    "seeding_time": t.get("seeding_time") or 0,
                    "state": state(t),
                    "upspeed": t["upload_payload_rate"],
                    "dlspeed": t["download_payload_rate"],
                    "added_on": int(t["time_added"]),
                    "category": t.get("label") or "",
                    "tracker": ((t.get("trackers") or [{}])[0]).get("url", ""),
                }
            )
        return out

    def remove(self, hashes: list[str], with_data: bool) -> None:
        for h in hashes:
            self.rpc("core.remove_torrent", h, bool(with_data))

    def export(self, h: str) -> bytes | None:
        return None  # Deluge can't hand out a torrent's .torrent file

    def add(self, content: bytes, filename: str, category: str) -> None:
        tid = self.rpc("core.add_torrent_file", filename, base64.b64encode(content).decode(), {})
        if tid and category:
            try:
                if category not in (self.rpc("label.get_labels") or []):
                    self.rpc("label.add", category)
                self.rpc("label.set_torrent", tid, category)
            except ServiceError:
                pass  # Label plugin not enabled

    def version(self) -> str:
        return "Deluge " + str(self.rpc("daemon.get_version"))
