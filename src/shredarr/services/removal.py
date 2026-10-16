"""Removing media everywhere, as a list of steps that can be shown first (the confirm dialog) and then run."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..clients.base import ServiceHTTPError
from ..config import JSON, Config
from ..text import episodes_label, tail
from .connections import Connections
from .deletes import Suppressions
from .media_server import media_server


@dataclass
class Step:
    desc: str
    run: Callable[[], Any] | None = None  # None: only a note

    @property
    def note(self) -> bool:
        return self.run is None


class MediaRemoval:
    def __init__(self, connections: Connections, suppressions: Suppressions) -> None:
        self.connections = connections
        self.suppressions = suppressions

    def seerr_step(self, kind: str, tmdb: int | None, title: str) -> Step | None:
        seerr = self.connections.seerr()
        if not tmdb or not seerr:
            return None
        try:
            info = seerr.media_info(kind, tmdb)
        except ServiceHTTPError:
            info = None
        return Step(f"Remove '{title}' from Seerr", lambda: seerr.delete_media(info["id"])) if info else None

    def jellyfin_delete(self, items: list[JSON]) -> None:
        jellyfin = self.connections.jellyfin()
        for i in items:
            self.suppressions.add_jellyfin_item(i["Id"])  # Jellyfin will report this delete too: it's handled already
            jellyfin.delete_item(i["Id"])

    def steps(
        self,
        c: Config,
        paths: Iterable[str],
        movies: Iterable[tuple[str, JSON]] = (),
        series: Iterable[tuple[str, JSON, list[JSON]]] = (),
    ) -> list[Step]:
        """Steps that remove media everywhere: the library files at paths from Jellyfin (which deletes them), movies
        from Radarr (and Seerr), and episodes in Sonarr (unmonitored, files removed; the series itself stays so new
        episodes keep coming). With Plex, Sonarr/Radarr delete the files and Plex rescans.
        movies = [(instance key, movie)], series = [(instance key, series, [episodes])]."""
        paths = set(paths)
        steps: list[Step | None] = []
        bh, server = c.behavior, media_server(self.connections)
        targets = {tail(p) for p in paths} if server.deletes_files else set()
        if targets:
            hits = [i for i in self.connections.jellyfin().media_items() if tail(i.get("Path") or "") in targets]
            if hits:
                eps = [i for i in hits if i.get("Type") == "Episode"]
                labels = [
                    f"'{n}' "
                    + episodes_label(
                        [
                            {
                                "seasonNumber": i.get("ParentIndexNumber") or 0,
                                "episodeNumber": i.get("IndexNumber") or 0,
                            }
                            for i in eps
                            if i.get("SeriesName") == n
                        ]
                    )
                    for n in sorted({i.get("SeriesName") for i in eps}, key=str)
                ]
                labels += [f"'{i['Name']}'" for i in hits if i.get("Type") != "Episode"]
                steps.append(
                    Step(
                        f"Delete {', '.join(labels)} from Jellyfin (and its library files)",
                        lambda: self.jellyfin_delete(hits),
                    )
                )
        for key, m in movies:
            excl, radarr = bh["import_exclusion"], self.connections.radarr(key)
            steps.append(
                Step(
                    f"Remove '{m['title']}' from {c.arr_name(key)}, with its files"
                    + (" (and add an import exclusion)" if excl else ""),
                    partial(radarr.delete_movie, m["id"], delete_files=True, add_exclusion=excl),
                )
            )
            if bh["remove_from_seerr"]:
                steps.append(self.seerr_step("movie", m.get("tmdbId"), m["title"]))
        for key, s, mine in series:
            label, sonarr = f"'{s['title']}' {episodes_label(mine)}", self.connections.sonarr(key)
            ids = sorted(e["id"] for e in mine)
            if bh["unmonitor"]:
                steps.append(
                    Step(
                        f"Unmonitor {label} in {c.arr_name(key)}",
                        partial(sonarr.set_monitored, ids, False),
                    )
                )
            files = sorted({e["episodeFileId"] for e in mine if e.get("episodeFileId")})
            if files:
                plex = not server.deletes_files
                steps.append(
                    Step(
                        f"{'Delete' if plex else 'Remove'} {label} files "
                        f"{'with' if plex else 'from'} {c.arr_name(key)}",
                        partial(sonarr.delete_episode_files, files),
                    )
                )
        if not server.deletes_files and paths:
            steps.append(
                Step(f"Rescan {server.name} so it drops the deleted files", partial(server.rescan, paths, deleted=True))
            )
        return [x for x in steps if x]
