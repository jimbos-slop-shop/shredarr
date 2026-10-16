"""Files on disk: videos, archives, hardlinks, and the paths other apps use for them."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from .config import JSON

VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")
ARCHIVE = re.compile(r"\.(rar|r\d\d|\d\d\d)$", re.I)
FileKey = tuple[int, int]


def files_under(path: str) -> list[str]:
    """The file itself, or every file in a folder (a torrent's content path can be either)."""
    return [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]


def videos_in(paths: Iterable[str]) -> list[str]:
    return [p for p in paths if p.lower().endswith(VIDEO) and "sample" not in os.path.basename(p).lower()]


def has_video(path: str) -> bool:
    return bool(videos_in(files_under(path)))


def has_archives(path: str) -> bool:
    return any(ARCHIVE.search(p) for p in files_under(path))


def restorable(path: str) -> str | None:
    """How a download can be imported again: "video" files, or "rar" archives to unpack first."""
    return "video" if has_video(path) else "rar" if has_archives(path) else None


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


def first_rar_volumes(paths: Iterable[str]) -> list[str]:
    """The first volume of each RAR set (name.rar or name.part1.rar), which unpacking starts from."""
    return [f for f in paths if re.search(r"\.rar$", f, re.I) and not re.search(r"\.part0*[2-9]\d*\.rar$", f, re.I)]


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
