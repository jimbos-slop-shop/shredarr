"""What Shredarr remembers about a movie or show before removing it, so Undo and Redownload can add it back with the
same settings."""

from __future__ import annotations

from ..config import JSON

MOVIE_KEYS = ("tmdbId", "title", "year", "qualityProfileId", "path", "monitored", "minimumAvailability", "tags")
SERIES_KEYS = ("tvdbId", "title", "qualityProfileId", "path", "seasonFolder", "seriesType", "monitored", "monitorNewItems", "tags")


def movie_settings(m: JSON) -> JSON:
    return {k: m.get(k) for k in MOVIE_KEYS}


def series_settings(s: JSON) -> JSON:
    return {k: s.get(k) for k in SERIES_KEYS}


def season_monitoring(s: JSON) -> list[list[int | bool]]:
    return [[x["seasonNumber"], x["monitored"]] for x in s["seasons"]]
