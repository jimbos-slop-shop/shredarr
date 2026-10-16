"""Settings → Jellyfin webhook: point Jellyfin's Webhook plugin at Shredarr."""

from __future__ import annotations

import base64
import secrets

from ..clients.base import ServiceError
from ..config import ConfigStore
from .activity import Activity
from .connections import Connections

WEBHOOK_PATH = "/webhook/jellyfin"

# What the plugin sends for each deleted item (a Handlebars template; Shredarr reads these fields).
TEMPLATE = """{
  "notification_type": "{{json_encode NotificationType}}",
  "item_type": "{{json_encode ItemType}}",
  "item_id": "{{json_encode ItemId}}",
  "name": "{{json_encode Name}}",
  "path": null,
  "tmdb_id": {{#if_exist Provider_tmdb}}{{Provider_tmdb}}{{else}}null{{/if_exist}},
  "tvdb_id": {{#if_exist Provider_tvdb}}{{Provider_tvdb}}{{else}}null{{/if_exist}},
  "imdb_id": {{#if_exist Provider_imdb}}"{{json_encode Provider_imdb}}"{{else}}null{{/if_exist}},
  "series_name": {{#if_exist SeriesName}}"{{json_encode SeriesName}}"{{else}}null{{/if_exist}},
  "series_id": {{#if_exist SeriesId}}"{{json_encode SeriesId}}"{{else}}null{{/if_exist}},
  "season_number": {{#if_exist SeasonNumber}}{{SeasonNumber}}{{else}}null{{/if_exist}},
  "episode_number": {{#if_exist EpisodeNumber}}{{EpisodeNumber}}{{else}}null{{/if_exist}},
  "episode_end_number": {{#if_exist EpisodeNumberEnd}}{{EpisodeNumberEnd}}{{else}}null{{/if_exist}},
  "occurred_at": "{{json_encode UtcTimestamp}}"
}"""


class WebhookSetup:
    def __init__(self, config: ConfigStore, connections: Connections, activity: Activity) -> None:
        self.config = config
        self.connections = connections
        self.activity = activity

    def token(self) -> str:
        """The shared secret Jellyfin sends along (X-Webhook-Token). Made on first start."""
        token = self.config.load().get("webhook_token")
        if not token:
            token = secrets.token_hex(32)
            self.config.update(lambda raw: raw.update(webhook_token=token))
        return str(token)

    def current_url(self) -> str | None:
        """The Shredarr address Jellyfin's webhook already uses, if it's set up."""
        jellyfin = self.connections.jellyfin()
        plugin = jellyfin.webhook_plugin()
        if not plugin:
            return None
        for o in jellyfin.plugin_configuration(plugin["Id"]).get("GenericOptions", []):
            uri = o.get("WebhookUri", "")
            if uri.endswith(WEBHOOK_PATH):
                return str(uri[: -len(WEBHOOK_PATH)])
        return None

    def setup(self, shredarr_url: str) -> str:
        """Create or update the Webhook plugin entry that sends Jellyfin deletes to Shredarr. Returns its address."""
        token = self.token()
        jellyfin = self.connections.jellyfin()
        plugin = jellyfin.webhook_plugin()
        if not plugin:
            raise ServiceError(
                "the Webhook plugin isn't installed in Jellyfin. Install it from Dashboard → Plugins → Catalog, "
                "restart Jellyfin, then try again"
            )
        conf = jellyfin.plugin_configuration(plugin["Id"])
        options = conf.setdefault("GenericOptions", [])
        target = shredarr_url.rstrip("/") + WEBHOOK_PATH
        mine = next((o for o in options if o.get("WebhookUri", "").endswith(WEBHOOK_PATH)), None)
        if mine is None:
            mine = {}
            options.append(mine)
        mine.update(
            {
                "NotificationTypes": ["ItemDeleted"],
                "WebhookName": "Shredarr",
                "WebhookUri": target,
                "EnableMovies": True,
                "EnableEpisodes": True,
                "EnableSeries": True,
                "EnableSeasons": True,
                "EnableAlbums": False,
                "EnableSongs": False,
                "EnableVideos": True,
                "SendAllProperties": False,
                "TrimWhitespace": False,
                "SkipEmptyMessageBody": False,
                "EnableWebhook": True,
                "Template": base64.b64encode(TEMPLATE.encode()).decode(),
                "UserFilter": [],
                "Headers": [{"Key": "X-Webhook-Token", "Value": token}],
                "Fields": [],
            }
        )
        jellyfin.save_plugin_configuration(plugin["Id"], conf)
        self.activity.info(f"Jellyfin webhook set up: deletes are sent to {target}")
        return target
