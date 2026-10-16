"""Seerr (Jellyseerr/Overseerr): only removing media and starting its library sync jobs."""

from __future__ import annotations

from ..config import JSON
from .base import JsonApi, ServiceHTTPError


class SeerrClient(JsonApi):
    app = "Seerr"

    def __init__(self, url: str, api_key: str) -> None:
        super().__init__(url, {"X-Api-Key": api_key})

    def version(self) -> str:
        self.get("/settings/main")  # needs a valid API key, unlike /status
        return str(self.get("/status").get("version") or "")

    def media_info(self, kind: str, tmdb_id: int) -> JSON | None:
        """Seerr's own record of a movie ("movie") or show ("tv"), if it has one."""
        try:
            info = self.get(f"/{kind}/{tmdb_id}").get("mediaInfo")
        except ServiceHTTPError as e:
            if e.status != 404:
                raise
            return None
        return dict(info) if info else None

    def delete_media(self, media_id: int) -> None:
        self.delete(f"/media/{media_id}")

    def sync(self, arr_kind: str) -> None:
        """Run the Sonarr or Radarr scan job, so Seerr notices media that came back."""
        self.post(f"/settings/jobs/{arr_kind}-scan/run")
