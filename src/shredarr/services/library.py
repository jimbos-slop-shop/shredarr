"""What's in the library, and which torrent each library file came from.

The cached maps returned here are shared: read them, don't change them.

A torrent counts as "in the library" while one of its video files is also a library file (a hardlink: the same
inode), or, for RAR releases (whose unpacked copy isn't linked to the torrent), while the file Sonarr/Radarr last
imported from it still exists. Answers are cached for a few minutes: walking the library and paging through
Sonarr/Radarr history is slow on big libraries.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..clients.base import ServiceError
from ..clients.servarr import DOWNLOAD_FOLDER_IMPORTED, SonarrClient
from ..clients.torrents import Torrent
from ..config import ARR_KINDS, JSON
from ..files import FileKey, file_key, files_under, videos_in
from .connections import Connections

log = logging.getLogger(__name__)

Import = dict[str, Any]  # {app, path, movieId, seriesId, episodeId}: one file Sonarr/Radarr imported from a torrent


@dataclass
class _Cache:
    t: float = 0
    value: Any = field(default_factory=dict)

    def fresh(self, max_age: float) -> bool:
        return time.time() - self.t < max_age


class Library:
    def __init__(self, connections: Connections) -> None:
        self.connections = connections
        self._roots = _Cache(value=[])
        self._imports = _Cache(value=({}, {}))
        self._index = _Cache(value=({}, False))
        self._groups = _Cache()
        self._titles = _Cache(value=({}, {}))
        self._in_library: dict[str, tuple[float, int]] = {}

    def forget(
        self, *, roots: bool = True, imports: bool = True, index: bool = True, groups: bool = True, titles: bool = True
    ) -> None:
        """Look again next time (after Shredarr changed things itself, or Settings changed)."""
        for flag, cache in (
            (roots, self._roots),
            (imports, self._imports),
            (index, self._index),
            (groups, self._groups),
            (titles, self._titles),
        ):
            if flag:
                cache.t = 0

    def forget_torrent(self, h: str) -> None:
        self._in_library.pop(h, None)

    # ------------------------------------------------------------------ library folders
    def root_dirs(self) -> list[str]:
        """Sonarr/Radarr root folders as Shredarr sees them (looked up every 5 minutes)."""
        if not self._roots.fresh(300):
            paths, dirs = self.connections.paths(), []
            for _, arr in self.connections.arrs():
                with contextlib.suppress(ServiceError):
                    dirs += [paths.local(r["path"].rstrip("/")) for r in arr.root_folders()]
            self._roots = _Cache(time.time(), sorted(set(dirs)))
        return list(self._roots.value)

    def library_path(self, p: str | None) -> str | None:
        """History keeps the path at import time. If a library root folder moved since (e.g. /media/Movies ->
        /data/Media/Movies), find the file under the current root folder of the same name."""
        if not p:
            return p
        p = self.connections.paths().local(p)
        if os.path.exists(p):
            return p
        for root in self.root_dirs():
            name = "/" + os.path.basename(root) + "/"
            i = p.find(name)
            if i >= 0:
                return root + p[i + len(name) - 1 :]
        return p

    def index(self, max_age: float = 300) -> dict[FileKey, str]:
        """(device, inode) -> path, for every video file in the Sonarr/Radarr root folders."""
        if time.time() - self._index.t > max_age:
            m, dirs = {}, self.root_dirs()
            for root in dirs:
                for d, _, fs in os.walk(root):
                    for p in videos_in(os.path.join(d, f) for f in fs):
                        if k := file_key(p):
                            m[k] = p
            self._index = _Cache(time.time(), (m, any(os.path.isdir(r) for r in dirs)))
        return self._index.value[0]  # type: ignore[no-any-return]

    @property
    def index_ok(self) -> bool:
        """The library folders could be read, so file ownership can be checked on disk."""
        return bool(self._index.value[1])

    # ------------------------------------------------------------------ Sonarr/Radarr import history
    def imports(self, max_age: float = 300) -> dict[str, list[Import]]:
        """torrent hash -> library imports recorded by Sonarr/Radarr."""
        self._load_imports(max_age)
        return self._imports.value[0]  # type: ignore[no-any-return]

    def import_owner(self, max_age: float = 300) -> dict[str, str]:
        """library path -> hash of the torrent it was last imported from."""
        self._load_imports(max_age)
        return self._imports.value[1]  # type: ignore[no-any-return]

    def _load_imports(self, max_age: float) -> None:
        if self._imports.fresh(max_age):
            return
        m: dict[str, list[Import]] = {}
        owner: dict[str, str] = {}
        for inst, arr in self.connections.arrs():
            try:
                for h in arr.history(
                    DOWNLOAD_FOLDER_IMPORTED, newest_first=True
                ):  # so the first import of a path is the one there now
                    if h.get("downloadId"):
                        path = self.library_path((h.get("data") or {}).get("importedPath"))
                        m.setdefault(h["downloadId"].lower(), []).append(
                            {
                                "app": inst.key,
                                "path": path,
                                "movieId": h.get("movieId"),
                                "seriesId": h.get("seriesId"),
                                "episodeId": h.get("episodeId"),
                            }
                        )
                        if path:
                            owner.setdefault(path, h["downloadId"].lower())
            except ServiceError as e:
                if inst.key in ARR_KINDS:
                    raise
                log.warning("history of %s: %s", inst.name, e)  # an extra instance being down shouldn't stop everything
        self._imports = _Cache(time.time(), (m, owner))

    def own_imports(self, h: str, max_age: float = 60) -> set[str]:
        """Library paths Sonarr/Radarr imported from torrent h that still exist and weren't replaced by a later import
        (an upgrade from another download)."""
        owner = self.import_owner(max_age)
        return {
            i["path"]
            for i in self.imports(max_age).get(h, [])
            if i["path"] and owner.get(i["path"]) == h and os.path.exists(i["path"])
        }

    def torrent_content(self, h: str) -> JSON:
        """Which movie or episodes a torrent was imported as (season packs and complete series included). Saved before
        anything is removed, because Sonarr/Radarr drop the history when a movie or show is deleted."""
        imps = self.imports(60).get(h, [])
        out: JSON = {"app": imps[0]["app"] if imps else None, "movieId": None, "seriesId": None, "episodes": []}
        ep_ids = {i["episodeId"] for i in imps if i.get("episodeId")}
        for i in imps:
            out["movieId"] = out["movieId"] or i.get("movieId")
            out["seriesId"] = out["seriesId"] or i.get("seriesId")
        if out["seriesId"] and ep_ids:
            try:
                sonarr = self.connections.arr(out["app"])
                assert isinstance(sonarr, SonarrClient)
                eps = sonarr.episodes(out["seriesId"])
                out["episodes"] = sorted([e["seasonNumber"], e["episodeNumber"]] for e in eps if e["id"] in ep_ids)
            except (ServiceError, AssertionError):
                pass
        return out

    # ------------------------------------------------------------------ torrents vs. library
    @staticmethod
    def torrent_keys(t: Torrent) -> set[FileKey]:
        return {k for p in videos_in(files_under(t["content_path"])) if (k := file_key(p))}

    def in_library(self, t: Torrent, max_age: float = 0) -> int:
        """How many library files are this torrent's: library files that are the same file on disk as one of the
        torrent's (hardlinks), or, for RAR releases, the paths Sonarr/Radarr last imported from it. A cross-seed of
        the same release counts too, since it shares the files."""
        hit = self._in_library.get(t["hash"])
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        idx, linked = self.index(), 0
        for p in videos_in(files_under(t["content_path"])):
            try:
                st = os.stat(p)
            except OSError:
                continue
            k = (st.st_dev, st.st_ino)
            if st.st_nlink > 1 and k not in idx and time.time() - self._index.t > 60:
                idx = self.index(60)  # maybe imported since the last scan
            if not self.index_ok:
                linked += st.st_nlink > 1  # library folders unknown (Sonarr/Radarr unreachable): any second link counts
            elif k in idx and file_key(idx[k]) == k:
                linked += 1
        n = max(linked, len(self.own_imports(t["hash"], max(max_age, 60))))
        self._in_library[t["hash"]] = (time.time(), n)
        return n

    def file_check(self, ts: list[Torrent]) -> Any:
        """A function telling whether a library file still belongs to one of these torrents: it is one of a torrent's
        files (hardlink), or it was last imported from a torrent that's still there (RAR releases). Files that can't
        be read count as belonging, so nothing is offered for deletion by mistake."""
        hashes = {t["hash"] for t in ts}
        keys = set().union(*[self.torrent_keys(t) for t in ts])
        owner = self.import_owner(60)

        def has_torrent(p: str) -> bool:
            k = file_key(p)
            return k is None or k in keys or owner.get(p) in hashes

        return has_torrent

    # ------------------------------------------------------------------ cross-seeds
    def cross_seed_groups(self, ts: list[Torrent], max_age: float = 60) -> dict[str, set[str]]:
        """hash -> every torrent sharing at least one video file with it: cross-seeds, the same release from other
        trackers, seeding from the same data or hardlinks to it. Torrents that share nothing aren't listed."""
        if self._groups.fresh(max_age):
            return self._groups.value  # type: ignore[no-any-return]
        parent: dict[str, str] = {}
        first: dict[FileKey, str] = {}

        def root(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = x = parent[parent[x]]
            return x

        for t in ts:
            for k in self.torrent_keys(t):
                if k in first:
                    parent[root(t["hash"])] = root(first[k])
                else:
                    first[k] = t["hash"]
        sets: dict[str, set[str]] = {}
        for h in list(parent):
            sets.setdefault(root(h), set()).add(h)
        m = {h: g for g in sets.values() if len(g) > 1 for h in g}
        self._groups = _Cache(time.time(), m)
        return m

    def group_original(self, group: set[str], ts: dict[str, Torrent]) -> str:
        """The torrent of a cross-seed group that Sonarr/Radarr imported (else the oldest). The others are its
        cross-seeds."""
        imap = self.imports()
        members = [h for h in group if h in ts] or list(group)  # ts loses torrents as they're deleted
        tracked = [h for h in members if h in imap]
        return min(tracked or members, key=lambda h: ts[h]["added_on"] if h in ts else 0)

    def cross_seed_of(self, h: str, ts: dict[str, Torrent], groups: dict[str, set[str]]) -> str | None:
        """The original torrent if h is a cross-seed of another torrent, else None."""
        g = groups.get(h)
        orig = self.group_original(g, ts) if g else None
        return orig if orig and orig != h and orig in ts else None

    # ------------------------------------------------------------------ titles
    def titles(self) -> tuple[dict[tuple[str, int], str], dict[tuple[str, int], str]]:
        """(series, movies): (instance key, Sonarr/Radarr id) -> title, refreshed every 5 minutes."""
        if not self._titles.fresh(300):
            series: dict[tuple[str, int], str] = {}
            movies: dict[tuple[str, int], str] = {}
            for inst, arr in self.connections.arrs():
                try:
                    if isinstance(arr, SonarrClient):
                        series.update({(inst.key, x["id"]): x["title"] for x in arr.all_series()})
                    else:
                        movies.update({(inst.key, x["id"]): x["title"] for x in arr.movies()})
                except ServiceError:
                    pass
            old_series, old_movies = self._titles.value
            self._titles = _Cache(time.time(), (series or old_series, movies or old_movies))
        return self._titles.value  # type: ignore[no-any-return]

    def title_of(self, h: str, pending_name: str | None = None) -> str | None:
        """The movie or show a torrent belongs to."""
        series, movies = self.titles()
        for i in self.imports().get(h, []):
            if (i["app"], i.get("seriesId")) in series:
                return series[(i["app"], i["seriesId"])]
            if (i["app"], i.get("movieId")) in movies:
                return movies[(i["app"], i["movieId"])]
        return re.sub(r"^'([^']+)'.*$", r"\1", pending_name) if pending_name else None
