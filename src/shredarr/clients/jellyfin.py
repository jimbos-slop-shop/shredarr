"""Jellyfin. The official Python client is built for players (device sign-in, websockets); a server-side tool with
an API key only needs these few calls."""

from __future__ import annotations

from typing import Any

from ..config import JSON
from .base import JsonApi

WEBHOOK_PLUGIN = "webhook"


def jf_id(item_id: str | None) -> str:
    """Jellyfin writes item ids with dashes in webhooks and without them in its API."""
    return (item_id or "").replace("-", "").lower()


class JellyfinClient(JsonApi):
    def __init__(self, url: str, api_key: str) -> None:
        super().__init__(url, {"X-Emby-Token": api_key})

    def system_info(self) -> JSON:
        return dict(self.get("/System/Info"))

    def items(self, **params: Any) -> list[JSON]:
        return list(self.get("/Items", **params)["Items"])

    def media_items(self) -> list[JSON]:
        """Every movie, episode and video, with its file path."""
        return self.items(recursive="true", fields="Path", includeItemTypes="Movie,Episode,Video", enableImages="false",
                          enableUserData="false")

    def provider_ids(self, item_id: str) -> dict[str, str]:
        """tvdb/tmdb/imdb ids of an item (lower-case keys), or {} if Jellyfin doesn't know it."""
        items = self.items(ids=item_id, fields="ProviderIds")
        return {k.lower(): v for k, v in (items[0].get("ProviderIds") or {}).items()} if items else {}

    def delete_item(self, item_id: str) -> None:
        """Deletes the item's files too."""
        self.delete(f"/Items/{item_id}")

    def library_locations(self) -> list[str]:
        return [loc for f in self.get("/Library/VirtualFolders") for loc in f.get("Locations", [])]

    def media_updated(self, updates: list[JSON]) -> None:
        self.post("/Library/Media/Updated", {"Updates": updates})

    def refresh_library(self) -> None:
        self.post("/Library/Refresh")

    def webhook_plugin(self) -> JSON | None:
        return next((p for p in self.get("/Plugins") if WEBHOOK_PLUGIN in p["Name"].lower()), None)

    def plugin_configuration(self, plugin_id: str) -> JSON:
        return dict(self.get(f"/Plugins/{plugin_id}/Configuration"))

    def save_plugin_configuration(self, plugin_id: str, conf: JSON) -> None:
        self.post(f"/Plugins/{plugin_id}/Configuration", conf)
