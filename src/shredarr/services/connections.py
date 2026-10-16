"""Clients for the apps set up in Settings → Connections, made from the current config."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from ..clients.base import NotConfigured, ServiceError, ServiceHTTPError
from ..clients.jellyfin import JellyfinClient
from ..clients.plex import PlexClient
from ..clients.seerr import SeerrClient
from ..clients.servarr import ArrClient, ProwlarrClient, RadarrClient, SonarrClient
from ..clients.torrents import Torrent, TorrentClient, make_client
from ..config import ARR_KINDS, CLIENTS, JSON, SERVICES, ArrInstance, ConfigStore, api_url, kind_of
from ..files import PathMapper

T = TypeVar("T")


class Connections:
    def __init__(self, config: ConfigStore) -> None:
        self.config = config
        self._clients: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.RLock()
        self.torrents = TorrentService(self)

    def paths(self) -> PathMapper:
        return PathMapper(self.config.load().path_maps)

    def reset(self) -> None:
        """Settings changed: make new clients next time."""
        with self._lock:
            self._clients.clear()
        self.torrents.reset()

    def _client(self, cls: Callable[[str, str], T], url: str, key: str) -> T:
        with self._lock:
            k = (getattr(cls, "__name__", str(cls)), url, key)
            if k not in self._clients:
                self._clients[k] = cls(url, key)
            return self._clients[k]  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ Sonarr / Radarr
    def arr_client(self, inst: ArrInstance) -> SonarrClient | RadarrClient:
        url, key = inst.conf["url"], inst.conf.get("api_key", "")
        if inst.kind == "sonarr":
            return self._client(SonarrClient, url, key)
        return self._client(RadarrClient, url, key)

    def arrs(self, kind: str | None = None) -> list[tuple[ArrInstance, SonarrClient | RadarrClient]]:
        return [(x, self.arr_client(x)) for x in self.config.load().arr_instances(kind)]

    def arr(self, key: str) -> SonarrClient | RadarrClient:
        """A Sonarr/Radarr instance by key. Falls back to the main instance of that kind if the instance was removed
        from Settings since."""
        insts = self.config.load().arr_instances()
        inst = next((x for x in insts if x.key == key), None) or next((x for x in insts if x.key == kind_of(key)), None)
        if not inst:
            raise NotConfigured(f"{SERVICES.get(kind_of(key), key)} isn't set up")
        return self.arr_client(inst)

    def sonarr(self, key: str) -> SonarrClient:
        client = self.arr(key)
        assert isinstance(client, SonarrClient)
        return client

    def radarr(self, key: str) -> RadarrClient:
        client = self.arr(key)
        assert isinstance(client, RadarrClient)
        return client

    # ------------------------------------------------------------------ the others
    def seerr(self) -> SeerrClient | None:
        conf = self.config.load().service("seerr")
        return self._client(SeerrClient, conf["url"], conf.get("api_key", "")) if conf.get("url") else None

    def prowlarr(self) -> ProwlarrClient | None:
        conf = self.config.load().service("prowlarr")
        return self._client(ProwlarrClient, conf["url"], conf.get("api_key", "")) if conf.get("url") else None

    def jellyfin(self) -> JellyfinClient:
        conf = self.config.load().service("jellyfin")
        return self._client(JellyfinClient, conf["url"].rstrip("/"), conf.get("api_key", ""))

    def plex(self) -> PlexClient:
        conf = self.config.load().service("plex")
        return self._client(PlexClient, conf["url"], conf.get("api_key", ""))


class TorrentService:
    """The torrent client over one shared session, made again when the client stops accepting it."""

    def __init__(self, connections: Connections) -> None:
        self.connections = connections
        self._client: TorrentClient | None = None
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self._client = None

    def _call(self, fn: Callable[[TorrentClient], T]) -> T:
        for attempt in (0, 1):
            with self._lock:
                try:
                    if self._client is None:
                        c = self.connections.config.load()
                        self._client = make_client(
                            c.client_kind, c.service(c.client_kind), self.connections.paths().local
                        )
                    return fn(self._client)
                except ServiceError as e:
                    self._client = None
                    signed_out = isinstance(e, ServiceHTTPError) and e.status in (401, 403, 409)
                    if attempt or not (signed_out or type(e) is ServiceError):
                        raise
        raise AssertionError("unreachable")

    def all(self, hashes: list[str] | str | None = None) -> list[Torrent]:
        """All torrents (or the given hashes), with paths as Shredarr sees them."""
        if isinstance(hashes, str):
            hashes = [hashes]
        ts = self._call(lambda c: c.torrents(hashes))
        paths = self.connections.paths()
        for t in ts:
            t["content_path"], t["save_path"] = paths.local(t["content_path"]), paths.local(t["save_path"])
        return ts

    def get(self, h: str) -> Torrent | None:
        return next(iter(self.all(h)), None)

    def remove(self, hashes: list[str], with_data: bool) -> None:
        self._call(lambda c: c.remove(hashes, with_data))

    def export(self, h: str) -> bytes | None:
        return self._call(lambda c: c.export(h))

    def add(self, content: bytes, filename: str, category: str) -> None:
        self._call(lambda c: c.add(content, filename, category))


def describe_service(svc: str, conf: JSON) -> str:
    """Can Shredarr reach this app with these settings? Returns what it is (e.g. "Sonarr 4.0.15"), or raises."""
    url = (conf or {}).get("url", "").strip().rstrip("/")
    if not url:
        raise ServiceError("no address set")
    svc = kind_of(svc) if kind_of(svc) in ARR_KINDS else svc
    if svc in ("sonarr", "radarr", "prowlarr"):
        r = ArrClient(api_url(svc, url), conf.get("api_key", "")).system_status()
        return f"{r.get('appName', SERVICES[svc])} {r.get('version', '')}".strip()
    if svc in CLIENTS:
        return make_client(svc, {"username": "", "password": "", **conf, "url": url}).version()
    if svc == "plex":
        return PlexClient(url, conf.get("api_key", "")).describe()
    if svc == "seerr":
        return "Seerr " + SeerrClient(api_url(svc, url), conf.get("api_key", "")).version()
    if svc == "jellyfin":
        r = JellyfinClient(url, conf.get("api_key", "")).system_info()
        return f"Jellyfin {r.get('Version', '')} · {r.get('ServerName', '')}"
    raise ServiceError("unknown service")
