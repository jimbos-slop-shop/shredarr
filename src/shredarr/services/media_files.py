"""Which files are media: videos to import, and RAR archives to unpack first. What counts as a video (and that a
"sample" doesn't) is Shredarr's rule, not a property of the file system."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from ..files import files_under

VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")
ARCHIVE = re.compile(r"\.(rar|r\d\d|\d\d\d)$", re.I)


def videos_in(paths: Iterable[str]) -> list[str]:
    return [p for p in paths if p.lower().endswith(VIDEO) and "sample" not in os.path.basename(p).lower()]


def restorable(path: str) -> str | None:
    """How a download can be imported again: "video" files, or "rar" archives to unpack first."""
    files = files_under(path)
    return "video" if videos_in(files) else "rar" if any(ARCHIVE.search(p) for p in files) else None


def first_rar_volumes(paths: Iterable[str]) -> list[str]:
    """The first volume of each RAR set (name.rar or name.part1.rar), which unpacking starts from."""
    return [
        f for f in paths if re.search(r"\.rar$", f, re.I) and not re.search(r"\.part(?!0*1\.rar$)\d+\.rar$", f, re.I)
    ]
