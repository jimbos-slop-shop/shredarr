"""Plex can't tell other apps about deletes, so with Plex Shredarr watches the library folders instead. A video file
that disappears and stays gone (it wasn't renamed, or replaced by an upgrade) is handled like a delete in Jellyfin."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable

from ..config import JSON
from ..files import file_key, videos_in
from .activity import Activity
from .deletes import Suppressions
from .library import Library

MASS_VANISH = (20, 0.2)  # more files than this, and more than this share of them, gone at once: a disk went offline


class LibraryWatcher:
    def __init__(
        self,
        library: Library,
        suppressions: Suppressions,
        activity: Activity,
        report: Callable[[JSON], None],
        state_file: str,
        confirm: float,
    ) -> None:
        self.library = library
        self.suppressions = suppressions
        self.activity = activity
        self.report = report  # gets {"disk_deletes": [paths]}, handled like a Jellyfin webhook event
        self.state_file = state_file
        self.confirm = confirm  # seconds a file must stay gone: an upgrade or rename replaces it within seconds
        self.files: dict[str, list[int]] | None = None
        self.missing: dict[str, float] = {}
        self.offline = False

    def library_files(self) -> dict[str, list[int]] | None:
        """path -> [device, inode] of every video file in the Sonarr/Radarr root folders, or None if one can't be
        read."""
        dirs = self.library.root_dirs()
        if not dirs or not all(os.path.isdir(d) for d in dirs):
            return None
        out = {}
        for root in dirs:
            for d, _, fs in os.walk(root):
                for p in videos_in(os.path.join(d, f) for f in fs):
                    if k := file_key(p):
                        out[p] = list(k)
        return out

    def previous(self) -> dict[str, list[int]] | None:
        if self.files is not None:
            return self.files
        try:
            with open(self.state_file) as f:
                return dict(json.load(f))
        except (OSError, ValueError):
            return None

    def look(self) -> None:
        """One look at the library: compare with the last one, and report files that stayed gone long enough."""
        now = self.library_files()
        if now is None:
            if not self.offline:
                self.activity.info(
                    "Library watch paused: a Sonarr/Radarr root folder can't be read (check the /data mount)"
                )
            self.offline = True
            return
        if self.offline:
            self.activity.info("Library watch resumed: all Sonarr/Radarr root folders can be read again")
        self.offline = False
        prev = self.previous()
        gone = [p for p in (prev or {}) if p not in now]
        if prev and len(gone) > MASS_VANISH[0] and len(gone) > MASS_VANISH[1] * len(prev):
            # a disk or share that went away isn't a delete: keep the old list and look again next time
            return self.activity.info(
                f"Library watch: {len(gone)} of {len(prev)} files vanished at once. That looks like a disk "
                "going offline, not a delete, so nothing was done"
            )
        self.files = now
        with open(self.state_file + ".tmp", "w") as f:
            json.dump(now, f)
        os.replace(self.state_file + ".tmp", self.state_file)
        moved = {tuple(k) for k in now.values()}
        t = time.time()
        for p in gone:
            if prev and tuple(prev[p]) not in moved and not self.suppressions.path(p):
                self.missing.setdefault(p, t)
        for p in list(self.missing):
            if p in now:
                self.missing.pop(p)
        ready = [p for p, since in self.missing.items() if t - since >= self.confirm]
        if ready:
            for p in ready:
                self.missing.pop(p)
            self.report({"disk_deletes": ready})
        return None
