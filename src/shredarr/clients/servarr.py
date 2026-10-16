"""Sonarr, Radarr and Prowlarr (the Servarr apps share most of their API).

Shredarr needs endpoints that pyarr doesn't cover (history filtered by event type, per-item history, /parse,
bulk episode file deletes), so these are thin clients over the documented v3/v1 APIs instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..config import JSON
from .base import JsonApi

GRABBED, DOWNLOAD_FOLDER_IMPORTED = 1, 3  # history event types


class ServarrClient(JsonApi):
    """url includes the API prefix (…/api/v3 or …/api/v1)."""

    def __init__(self, url: str, api_key: str) -> None:
        super().__init__(url, {"X-Api-Key": api_key})

    def system_status(self) -> JSON:
        return dict(self.get("/system/status"))


class ArrClient(ServarrClient):
    """What Sonarr and Radarr share."""

    def root_folders(self) -> list[JSON]:
        return list(self.get("/rootfolder"))

    def quality_profiles(self) -> list[JSON]:
        return list(self.get("/qualityprofile"))

    def history(self, event_type: int, *, newest_first: bool = False, page_size: int = 1000) -> Iterator[JSON]:
        """Every history record of one event type, page by page."""
        page = 1
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": page_size, "eventType": event_type}
            if newest_first:
                params.update(sortKey="date", sortDirection="descending")
            r = self.get("/history", **params)
            yield from r["records"]
            if page * page_size >= r["totalRecords"]:
                return
            page += 1

    def command(self, name: str, **body: Any) -> JSON:
        return dict(self.post("/command", {"name": name, **body}))

    def command_status(self, command_id: int) -> JSON:
        return dict(self.get(f"/command/{command_id}"))

    def parse(self, title: str) -> JSON:
        """What Sonarr/Radarr make of a release name."""
        return dict(self.get("/parse", title=title))


class RadarrClient(ArrClient):
    def movies(self, tmdb_id: int | None = None) -> list[JSON]:
        return list(self.get("/movie", tmdbId=tmdb_id) if tmdb_id else self.get("/movie"))

    def movie(self, movie_id: int) -> JSON:
        return dict(self.get(f"/movie/{movie_id}"))

    def add_movie(self, body: JSON) -> JSON:
        return dict(self.post("/movie", body))

    def update_movie(self, movie: JSON) -> None:
        self.put(f"/movie/{movie['id']}", movie)

    def delete_movie(self, movie_id: int, *, delete_files: bool, add_exclusion: bool) -> None:
        self.delete(f"/movie/{movie_id}", deleteFiles=str(delete_files).lower(), addImportExclusion=str(add_exclusion).lower())

    def movie_history(self, movie_id: int) -> list[JSON]:
        return list(self.get("/history/movie", movieId=movie_id))

    def lookup_tmdb(self, tmdb_id: int) -> JSON:
        return dict(self.get("/movie/lookup/tmdb", tmdbId=tmdb_id))

    def lookup(self, term: str) -> list[JSON]:
        return list(self.get("/movie/lookup", term=term))

    def exclusions(self) -> list[JSON]:
        return list(self.get("/exclusions"))

    def delete_exclusion(self, exclusion_id: int) -> None:
        self.delete(f"/exclusions/{exclusion_id}")


class SonarrClient(ArrClient):
    def all_series(self) -> list[JSON]:
        return list(self.get("/series"))

    def series(self, series_id: int) -> JSON:
        return dict(self.get(f"/series/{series_id}"))

    def add_series(self, body: JSON) -> JSON:
        return dict(self.post("/series", body))

    def update_series(self, series: JSON) -> None:
        self.put(f"/series/{series['id']}", series)

    def delete_series(self, series_id: int, *, delete_files: bool, add_exclusion: bool) -> None:
        self.delete(f"/series/{series_id}", deleteFiles=str(delete_files).lower(),
                    addImportListExclusion=str(add_exclusion).lower())

    def series_history(self, series_id: int) -> list[JSON]:
        return list(self.get("/history/series", seriesId=series_id))

    def lookup(self, term: str) -> list[JSON]:
        return list(self.get("/series/lookup", term=term))

    def episodes(self, series_id: int) -> list[JSON]:
        return list(self.get("/episode", seriesId=series_id))

    def set_monitored(self, episode_ids: list[int], monitored: bool) -> None:
        self.put("/episode/monitor", {"episodeIds": episode_ids, "monitored": monitored})

    def set_seasons_monitored(self, series_id: int, seasons: set[int], monitored: bool) -> None:
        series = self.series(series_id)
        for x in series["seasons"]:
            if x["seasonNumber"] in seasons:
                x["monitored"] = monitored
        self.update_series(series)

    def episode_files(self, series_id: int) -> list[JSON]:
        return list(self.get("/episodefile", seriesId=series_id))

    def delete_episode_files(self, file_ids: list[int]) -> None:
        self.delete("/episodefile/bulk", {"episodeFileIds": file_ids})

    def import_list_exclusions(self) -> list[JSON]:
        return list(self.get("/importlistexclusion"))

    def delete_import_list_exclusion(self, exclusion_id: int) -> None:
        self.delete(f"/importlistexclusion/{exclusion_id}")


class ProwlarrClient(ServarrClient):
    def indexers(self) -> list[JSON]:
        return list(self.get("/indexer"))
