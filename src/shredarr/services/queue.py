"""The cleanup queue: torrents whose media is gone, waiting until their seeding rule is met."""

from __future__ import annotations

from collections.abc import Iterable

from ..config import JSON
from ..repositories import PendingRepo
from .activity import Activity
from .library import Library


def merge_undo(a: JSON | None, b: JSON) -> JSON:
    """Two episode deletes that share a season-pack torrent: undo should bring both back."""
    if not a:
        return b
    if a.get("kind") == b.get("kind") == "episodes" and a.get("series_id") == b.get("series_id"):
        a["remonitor"] = sorted(set(a["remonitor"]) | set(b["remonitor"]))
        a["seasons"] = sorted(set(a["seasons"]) | set(b["seasons"]))
    return a


class CleanupQueue:
    def __init__(self, pending: PendingRepo, library: Library, activity: Activity) -> None:
        self.pending = pending
        self.library = library
        self.activity = activity

    def add(self, hashes: Iterable[str], name: str, source: str, undo: JSON | None = None, event: str | None = None) -> int:
        """Queue torrents (download ids from Sonarr/Radarr history). Returns how many weren't queued already."""
        hashes = list(hashes)
        new = 0
        for h in (x.lower() for x in hashes):
            existing = self.pending.get(h)
            if existing is None:
                try:
                    content: JSON | None = self.library.torrent_content(h)
                except Exception:  # best effort: what it was is only needed later, for Redownload
                    content = None
                self.pending.add(h, name, source, undo=undo, event=event, content=content)
                new += 1
            elif undo:
                self.pending.set_undo(h, merge_undo(existing.undo, undo))
        self.activity.info(f"  {len(hashes)} torrent(s) found in history, {new} newly queued for cleanup after seeding")
        return new
