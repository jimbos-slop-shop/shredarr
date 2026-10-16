"""Tracker names (e.g. TorrentDay) for torrents, from Sonarr/Radarr's grab history and Prowlarr."""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Any

from ..clients.base import ServiceError
from ..clients.servarr import GRABBED
from ..clients.torrents import Torrent
from .connections import Connections

log = logging.getLogger(__name__)


def tracker_host(t: Torrent) -> str:
    """Only the tracker's host name. Never the full announce URL: it contains the user's private passkey."""
    host = urllib.parse.urlparse(t.get("tracker") or "").hostname or ""
    return host[4:] if host.startswith("www.") else host


def site_of(host: str | None) -> str:
    """tracker.example.org -> example.org (good enough to match an announce host to a Prowlarr site)."""
    parts = (host or "").split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and len(parts[-1]) == 2 and len(parts[-2]) <= 3 else parts[-2:])


class Trackers:
    def __init__(self, connections: Connections) -> None:
        self.connections = connections
        self._indexers: dict[str, Any] = {"t": 0, "by_hash": {}, "prowlarr": []}
        self._names: tuple[float, dict[str, str]] = (0, {})

    def forget(self) -> None:
        self._indexers["t"] = 0

    def indexers(self, max_age: float = 600) -> dict[str, Any]:
        """{"by_hash": hash -> indexer it was grabbed from,
        "prowlarr": [{name, hosts}] of Prowlarr's torrent indexers}."""
        if time.time() - self._indexers["t"] < max_age:
            return self._indexers
        by_hash: dict[str, str] = {}
        prowlarr: list[dict[str, Any]] = []
        for _, arr in self.connections.arrs():
            try:
                for h in arr.history(GRABBED):
                    name = re.sub(r"\s*\(Prowlarr\)$", "", (h.get("data") or {}).get("indexer") or "")
                    if h.get("downloadId") and name:
                        by_hash.setdefault(h["downloadId"].lower(), name)
            except ServiceError:
                pass
        client = self.connections.prowlarr()
        if client:
            try:
                for i in client.indexers():
                    if i.get("protocol") == "torrent":
                        urls = [f.get("value") for f in i.get("fields", []) if f.get("name") == "baseUrl"] + (
                            i.get("indexerUrls") or []
                        )
                        hosts = [urllib.parse.urlparse(u).hostname for u in urls if u]
                        prowlarr.append(
                            {
                                "name": i["name"],
                                "hosts": sorted({h[4:] if h.startswith("www.") else h for h in hosts if h}),
                            }
                        )
            except ServiceError as e:
                log.warning("prowlarr: %s", e)
        self._indexers = {"t": time.time(), "by_hash": by_hash, "prowlarr": prowlarr}
        return self._indexers

    def names(self, ts: list[Torrent] | None = None, max_age: float = 60) -> dict[str, str]:
        """hash -> tracker name. From the grab history; torrents that weren't grabbed by Sonarr/Radarr get the name
        other torrents with the same announce host have, or a Prowlarr indexer on the same site."""
        if ts is None and time.time() - self._names[0] < max_age:
            return self._names[1]
        if ts is None:
            ts = self.connections.torrents.all()
        info, out = self.indexers(), {}
        by_host_all: dict[str, list[str]] = {}
        for t in ts:
            if n := info["by_hash"].get(t["hash"]):
                by_host_all.setdefault(tracker_host(t), []).append(n)
        by_host = {h: max(set(ns), key=ns.count) for h, ns in by_host_all.items()}
        by_site = {site_of(h): i["name"] for i in info["prowlarr"] for h in i["hosts"]}
        for t in ts:
            host = tracker_host(t)
            n = info["by_hash"].get(t["hash"]) or by_host.get(host) or by_site.get(site_of(host))
            if n:
                out[t["hash"]] = n
        self._names = (time.time(), out)
        return out

    def choices(self, ts: list[Torrent]) -> list[dict[str, str]]:
        """Suggestions for tracker rules: tracker names (from grab history and Prowlarr), then announce hosts."""
        if not ts:
            return []
        names = self.names(ts)
        hosts: dict[str, set[str | None]] = {}
        for t in ts:
            if host := tracker_host(t):
                hosts.setdefault(host, set()).add(names.get(t["hash"]))
        by_name: dict[str, set[str]] = {}
        for t in ts:
            if n := names.get(t["hash"]):
                by_name.setdefault(n, set()).add(tracker_host(t))
        out = [
            {"value": n, "label": f"{n} · {', '.join(sorted(h - {''}))}"}
            for n, h in sorted(by_name.items(), key=lambda x: x[0].casefold())
        ]
        out += [
            {"value": i["name"], "label": f"{i['name']} · Prowlarr, no torrents yet"}
            for i in self.indexers()["prowlarr"]
            if i["name"] not in by_name
        ]
        out += [
            {"value": h, "label": h + (f" · {', '.join(sorted(x for x in n if x))}" if n - {None} else "")}
            for h, n in sorted(hosts.items())
        ]
        return out
