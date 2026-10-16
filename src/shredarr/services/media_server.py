"""Telling the media server about files Shredarr brought back or removed. Jellyfin and Plex behind one interface."""

from __future__ import annotations

import logging
import os
import threading
from typing import Protocol

from ..clients.base import ServiceError
from ..config import JSON
from .connections import Connections

log = logging.getLogger(__name__)
JELLYFIN_RETRY = 90  # seconds: Jellyfin can ignore the first notice when the file's folder is brand new


def map_onto(p: str, location: str) -> str | None:
    """p (a library file as Shredarr sees it) under one of the media server's own library folders, matched by the
    folder's name: the media server may mount the library at another path."""
    name = "/" + os.path.basename(location.rstrip("/")) + "/"
    i = p.find(name)
    return location.rstrip("/") + p[i + len(name) - 1 :] if i >= 0 else None


class MediaServer(Protocol):
    name: str
    deletes_files: bool  # True if deleting an item in it deletes its files (Jellyfin); else Sonarr/Radarr do (Plex)

    def rescan(self, paths: list[str] | set[str], deleted: bool = False) -> None:
        """Tell it about restored (or deleted) library files."""

    def refresh_all(self) -> None: ...


class Jellyfin:
    name = "Jellyfin"
    deletes_files = True

    def __init__(self, connections: Connections) -> None:
        self.connections = connections

    def rescan(self, paths: list[str] | set[str], deleted: bool = False) -> None:
        """Only restored files: Jellyfin notices deletes itself. Sent twice (see JELLYFIN_RETRY)."""
        if deleted:
            return
        paths = list(paths)
        self.notify_created(paths)
        timer = threading.Timer(JELLYFIN_RETRY, self.notify_created, args=(paths,))
        timer.daemon = True
        timer.start()

    def notify_created(self, paths: list[str]) -> None:
        """For setups where Sonarr/Radarr don't tell Jellyfin about imports themselves."""
        jellyfin = self.connections.jellyfin()
        try:
            locations = jellyfin.library_locations()
        except ServiceError as e:
            return log.warning("jellyfin rescan: %s", e)
        updates: list[JSON] = []
        for p in paths:
            for loc in locations:
                jp = map_onto(p, loc)
                if jp:
                    updates += [
                        {"Path": os.path.dirname(jp), "UpdateType": "Created"},
                        {"Path": jp, "UpdateType": "Created"},
                    ]
                    break
        if updates:
            try:
                jellyfin.media_updated(updates)
            except ServiceError as e:
                log.warning("jellyfin rescan: %s", e)
        return None

    def refresh_all(self) -> None:
        self.connections.jellyfin().refresh_library()


class Plex:
    name = "Plex"
    deletes_files = False

    def __init__(self, connections: Connections) -> None:
        self.connections = connections

    def rescan(self, paths: list[str] | set[str], deleted: bool = False) -> None:
        """Partial scans of the folders restored files are in. After deletes, the whole library section is scanned
        instead: Plex merges a movie's versions from different folders (1080p and 4K) into one item, and a scan of only
        one folder would drop the other version too until its folder is scanned."""
        plex = self.connections.plex()
        try:
            sections = plex.sections()
        except ServiceError as e:
            return log.warning("plex rescan: %s", e)
        todo: set[tuple[str, str | None]] = set()
        for p in paths:
            for s in sections:
                for loc in s.locations:
                    mapped = map_onto(p, loc)
                    if mapped:
                        todo.add((s.key, None if deleted else os.path.dirname(mapped)))
        for key, folder in sorted(todo, key=str):
            try:
                plex.scan(key, folder)
            except ServiceError as e:
                log.warning("plex rescan: %s", e)
        return None

    def refresh_all(self) -> None:
        self.connections.plex().scan_all()


def media_server(connections: Connections) -> Jellyfin | Plex:
    """The media server set up in Settings."""
    return Plex(connections) if connections.config.load().media_kind == "plex" else Jellyfin(connections)
