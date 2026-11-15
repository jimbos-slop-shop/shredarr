"""Leftovers: torrents Sonarr/Radarr imported from whose files are no longer in the library, usually because an upgrade
replaced them. The dashboard lists them, and with Settings → "Clean up leftovers automatically" the sweep puts them
on the cleanup list by itself."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Container

from ..clients.base import ServiceError
from ..clients.torrents import Torrent
from ..config import Config, Rule
from ..text import episodes_text
from .activity import Activity
from .library import Import, Library
from .queue import CleanupQueue
from .rules import rule_for
from .trackers import Trackers
from .watcher import MASS_VANISH

Torrents = dict[str, Torrent]


def is_managed(h: str, imports: dict[str, list[Import]], groups: dict[str, set[str]]) -> bool:
    """Sonarr/Radarr imported from this torrent, or from a cross-seed of it."""
    return h in imports or any(x in imports for x in groups.get(h, ()))


def is_leftover(
    library: Library, t: Torrent, imports: dict[str, list[Import]], groups: dict[str, set[str]], queued: Container[str]
) -> bool:
    """A finished torrent Sonarr/Radarr imported from, not queued yet, with none of its files left in the library."""
    h = t["hash"]
    return (
        is_managed(h, imports, groups)
        and h not in queued
        and t["progress"] >= 1
        and not library.in_library(t, max_age=60)  # last: it reads the torrent's files (cached for a minute)
    )


def too_many(n: int, of: int) -> bool:
    """So many at once that it looks like a disk going offline, not upgrades (the library watch's own limit)."""
    return n > MASS_VANISH[0] and n > MASS_VANISH[1] * of


class AutoLeftovers:
    """Puts leftovers on the cleanup list by itself. A disk that goes offline makes every torrent look like a
    leftover, so this holds back whenever that could be what's happening: a library folder can't be read, the library
    watch saw files vanish at once, or many torrents turn into leftovers together. A torrent is also only queued once
    it has been a leftover for a while (wait, in seconds), so a library that's briefly half there doesn't count."""

    def __init__(
        self,
        library: Library,
        trackers: Trackers,
        queue: CleanupQueue,
        activity: Activity,
        wait: float,
        disk_trouble: Callable[[], bool],
    ) -> None:
        self.library = library
        self.trackers = trackers
        self.queue = queue
        self.activity = activity
        self.wait = wait
        self.disk_trouble = disk_trouble  # the library watch thinks a disk is offline
        self.since: dict[str, float] = {}  # hash -> when it was first seen as a leftover (a restart starts over)
        self.dry_run_said: set[str] = set()  # leftovers a dry run already said it would add, so it's said once
        self.too_many_said = False  # the "too many at once" problem was reported, so it's said once until it clears

    def run(
        self, c: Config, ts: Torrents, groups: dict[str, set[str]], overrides: dict[str, Rule], queued: set[str]
    ) -> bool:
        """One look for leftovers that are ready to go. Returns whether anything was queued."""
        if self.disk_trouble() or not self.library_readable():
            self.since.clear()  # unsure what's a leftover: the wait starts over once the library is back
            return False
        try:
            found = self.leftovers(c, ts, groups, overrides, queued)
            imports = self.library.imports()
        except ServiceError:  # Sonarr/Radarr can't be asked: unsure what's in the library, so try again next sweep
            return False
        if self.disk_trouble():  # the library watch may have looked while this was reading the torrents' files
            self.since.clear()
            return False
        now = time.time()
        for h in list(self.since):
            if h not in found:
                self.since.pop(h)
        self.dry_run_said &= set(found)
        ready = [h for h in found if now - self.since.setdefault(h, now) >= self.wait]
        managed = sum(is_managed(h, imports, groups) for h in ts)
        if too_many(len(ready), managed):
            if not self.too_many_said:
                self.too_many_said = True
                self.activity.error(
                    f"{len(ready)} of {managed} torrents turned into leftovers at once. That looks like a disk going "
                    "offline, not upgrades, so none were added to the cleanup list. If they really are leftovers, "
                    "add them from Leftovers on the dashboard"
                )
            return False
        self.too_many_said = False
        added = False
        for h in ready:
            if h in self.dry_run_said:
                continue
            name = self.library.title_of(h) or ts[h]["name"]
            what = self.describe(h, name)
            if c.dry_run:
                self.dry_run_said.add(h)
                self.activity.info(f"[dry run] would have added {what} to the cleanup list: its files were replaced")
                continue
            self.activity.info(
                f"Added {what} to the cleanup list automatically: its files were replaced in the library"
            )
            self.queue.add([h], name, "leftover")
            self.since.pop(h)
            added = True
        return added

    def library_readable(self) -> bool:
        """Every Sonarr/Radarr root folder can be read (a missing one makes all its torrents look like leftovers)."""
        dirs = self.library.root_dirs()
        return bool(dirs) and all(os.path.isdir(d) for d in dirs)

    def leftovers(
        self, c: Config, ts: Torrents, groups: dict[str, set[str]], overrides: dict[str, Rule], queued: set[str]
    ) -> list[str]:
        """Leftovers that may be queued: not protected (rule "never"), and not a cross-seed of another torrent (those
        follow their original onto the list)."""
        imports, out = self.library.imports(), []
        for h, t in ts.items():
            if not is_leftover(self.library, t, imports, groups, queued) or self.library.cross_seed_of(h, ts, groups):
                continue
            if rule_for(t, overrides, c, self.library.title_of(h), self.trackers.names)["mode"] == "never":
                continue
            out.append(h)
        return out

    def describe(self, h: str, name: str) -> str:
        """'Paper Moons' S01E01, or 'Spring' for a movie."""
        try:
            eps = self.library.torrent_content(h).get("episodes") or []
        except ServiceError:
            eps = []
        return f"'{name}'" + (f" {episodes_text(eps)}" if eps else "")
