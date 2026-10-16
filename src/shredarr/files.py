"""Files on disk: walking folders, hardlinks, and the paths other apps use for them."""

from __future__ import annotations

import os

from .config import JSON

FileKey = tuple[int, int]


def files_under(path: str) -> list[str]:
    """The file itself, or every file in a folder (a torrent's content path can be either)."""
    return [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]


def file_key(path: str) -> FileKey | None:
    """A file's identity on disk. Hardlinks to the same data share it, whatever their paths."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def nlink(path: str) -> int:
    try:
        return os.stat(path).st_nlink
    except OSError:
        return 0


class PathMapper:
    """Paths as the torrent client, Sonarr and Radarr report them <-> the same files as Shredarr sees them
    (Settings → Path mappings)."""

    def __init__(self, maps: list[JSON]) -> None:
        self.maps = maps

    def local(self, p: str | None) -> str:
        for m in self.maps:
            if p and m.get("remote") and p.startswith(m["remote"]):
                return str(m["local"]) + p[len(m["remote"]) :]
        return p or ""

    def remote(self, p: str) -> str:
        for m in self.maps:
            if p and m.get("local") and p.startswith(m["local"]):
                return str(m["remote"]) + p[len(m["local"]) :]
        return p

    def on_disk(self, p: str | None) -> bool:
        """Does this file (as Sonarr/Radarr name it) still exist?"""
        return bool(p) and os.path.exists(self.local(p))
