"""Deletes Shredarr made itself, remembered so the media server's report of them isn't handled a second time."""

from __future__ import annotations

import threading
import time

from ..clients.jellyfin import jf_id


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
