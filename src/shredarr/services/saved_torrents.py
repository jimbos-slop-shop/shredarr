"""The .torrent files Shredarr keeps when it deletes a torrent for good, so History → Redownload can add the same
release back. They're removed once they're older than Settings → Saved .torrent files allows (1 year by default).
Saving can be switched off there; files already saved then stay until they expire or are deleted."""

from __future__ import annotations

import glob
import os
import time

from ..config import JSON, ConfigStore
from ..text import human, plural
from .activity import Activity


def retention_text(value: float, unit: str) -> str:
    return "forever" if unit == "forever" else f"{value:g} {unit[:-1] if value == 1 else unit}"


class SavedTorrents:
    def __init__(self, folder: str, config: ConfigStore, activity: Activity) -> None:
        self.folder = folder
        self.config = config
        self.activity = activity

    def path(self, h: str | None) -> str:
        return os.path.join(self.folder, (h or "") + ".torrent")

    def has(self, h: str | None) -> bool:
        return bool(h) and os.path.isfile(self.path(h))

    @property
    def enabled(self) -> bool:
        return self.config.load().save_torrents

    def save(self, h: str, data: bytes) -> None:
        os.makedirs(self.folder, exist_ok=True)
        tmp = self.path(h) + ".tmp"  # written in full first: a cut-off .torrent would look like a saved one
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.path(h))

    def files(self) -> list[str]:
        return sorted(glob.glob(os.path.join(glob.escape(self.folder), "*.torrent")))

    def _unfinished(self, older_than: float = 0) -> list[str]:
        """Temporary files left behind by a save that was cut off (Shredarr stopped while writing)."""
        cutoff = time.time() - older_than
        found = glob.glob(os.path.join(glob.escape(self.folder), "*.torrent.tmp"))
        return [p for p in found if os.path.getmtime(p) <= cutoff]

    def summary(self) -> JSON:
        """How many are kept and how much space they take, for Settings."""
        sizes = [os.path.getsize(p) for p in self.files()]
        return {"count": len(sizes), "bytes": sum(sizes)}

    def _remove(self, paths: list[str]) -> tuple[int, int]:
        n = size = 0
        for p in paths:
            try:
                size += os.path.getsize(p)
                os.remove(p)
                n += 1
            except FileNotFoundError:
                pass
        return n, size

    def prune(self) -> int:
        """Remove saved files older than the setting allows. Returns how many went."""
        self._remove(self._unfinished(older_than=3600))  # an hour old: not one being written right now
        keep = self.config.load().torrent_retention
        seconds = self.config.load().torrent_retention_seconds
        if seconds is None:
            return 0
        cutoff = time.time() - seconds
        n, _ = self._remove([p for p in self.files() if os.path.getmtime(p) < cutoff])
        if n:
            self.activity.info(
                f"Removed {plural(n, 'saved .torrent file')} older than {retention_text(keep['value'], keep['unit'])}: "
                "Redownload of those can only search again"
            )
        return n

    def delete_all(self) -> int:
        self._remove(self._unfinished())
        n, size = self._remove(self.files())
        self.activity.info(
            f"Deleted all saved .torrent files ({plural(n, 'file')}, {human(size)}): "
            "Redownload of earlier cleanups can only search again"
        )
        return n
