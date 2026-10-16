"""config.json: everything set up in Settings (connections, rules, notifications, ...).

The file is read again whenever it's needed, so changes saved in the web UI apply right away.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

JSON = dict[str, Any]

MODES = {
    "either": "seed time OR ratio (whichever first)",
    "both": "seed time AND ratio",
    "days": "seed time only",
    "ratio": "ratio only",
    "never": "never delete (protected)",
}
CLIENTS = {"qbittorrent": "qBittorrent", "transmission": "Transmission", "deluge": "Deluge"}
MEDIA_SERVERS = {"jellyfin": "Jellyfin", "plex": "Plex"}
SERVICES = {
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "seerr": "Seerr",
    **CLIENTS,
    **MEDIA_SERVERS,
    "prowlarr": "Prowlarr",
}
ARR_KINDS = ("sonarr", "radarr")
OPTIONAL = ("seerr", "prowlarr")
SECRET_KEYS = ("api_key", "password")  # never sent back to the browser
API_SUFFIX = {"sonarr": "/api/v3", "radarr": "/api/v3", "seerr": "/api/v1", "prowlarr": "/api/v1"}

BEHAVIOR_DEFAULTS = {
    "import_exclusion": True,
    "remove_from_seerr": True,
    "unmonitor": True,
    "remove_episode_files": True,
    "cross_seed_with_original": False,
}
LOW_SPACE_DEFAULTS: JSON = {"enabled": False, "min_free_gb": 100}
POLICY_DEFAULTS: JSON = {"min_length": 8, "upper": False, "digit": False, "symbol": False}
NOTIFY_DEFAULTS: JSON = {
    "discord_url": "",
    "discord_username": "Shredarr",
    "discord_avatar": "",
    "ntfy_server": "",
    "ntfy_topic": "",
    "ntfy_token": "",
    "ntfy_priority": "3",
    "pushover_user": "",
    "pushover_token": "",
    "pushover_priority": "0",
    "pushover_sound": "",
    "on_cleanup": True,
    "on_failure": True,
    "on_jellyfin": False,
    "on_undo": True,
    "daily_summary": True,
    "summary_time": "09:00",
}
DEFAULT_RULE: JSON = {"mode": "either", "min_seed_days": 14, "min_ratio": 2}
RETENTION_UNITS = {"days": 86400, "months": 30 * 86400, "years": 365 * 86400, "forever": None}
RETENTION_DEFAULT: JSON = {"value": 1, "unit": "years"}


class Rule(TypedDict, total=False):
    mode: str
    min_seed_days: float | None
    min_ratio: float | None
    source: str  # where the rule came from: "default", "this torrent", or the matching show/tracker/category


def kind_of(key: str | None) -> str:
    """'sonarr' or 'radarr' for an instance key ("radarr", "radarr-4k", ...)."""
    return (key or "").split("-")[0]


def service_kind(svc: str) -> str:
    """Which app a Settings entry is: "radarr" for "radarr-4k"; any other service as it is."""
    return kind_of(svc) if kind_of(svc) in ARR_KINDS else svc


def api_suffix(svc: str) -> str:
    return API_SUFFIX.get(svc) or API_SUFFIX.get(kind_of(svc), "")


def base_url(svc: str, url: str | None) -> str:
    """What the user typed (http://sonarr:8989) -> what the API client needs (…/api/v3)."""
    url = (url or "").strip().rstrip("/")
    suffix = api_suffix(svc)
    if suffix and url.endswith(suffix):
        url = url[: -len(suffix)]
    return url


def api_url(svc: str, url: str | None) -> str:
    return base_url(svc, url) + api_suffix(svc) if url else ""


@dataclass(frozen=True)
class ArrInstance:
    """A Sonarr or Radarr: the main ones have the keys "sonarr" and "radarr", extra ones keys like "radarr-4k"."""

    key: str
    kind: str
    name: str
    conf: JSON


class Config:
    """A read-only view of config.json with the defaults applied."""

    def __init__(self, raw: JSON) -> None:
        self.raw = raw

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def service(self, key: str) -> JSON:
        return dict(self.raw.get(key) or {})

    @property
    def dry_run(self) -> bool:
        return bool(self.raw.get("dry_run"))

    # ------------------------------------------------------------------ connections
    @property
    def client_kind(self) -> str:
        """Which torrent client is used: qbittorrent, transmission or deluge."""
        k = self.raw.get("torrent_client")
        return k if k in CLIENTS else "qbittorrent"

    @property
    def media_kind(self) -> str:
        """Which media server is used: jellyfin or plex."""
        k = self.raw.get("media_server")
        return k if k in MEDIA_SERVERS else "jellyfin"

    @property
    def client_name(self) -> str:
        return CLIENTS[self.client_kind]

    @property
    def media_name(self) -> str:
        return MEDIA_SERVERS[self.media_kind]

    @property
    def required(self) -> list[str]:
        return ["sonarr", "radarr", self.client_kind, self.media_kind]

    @property
    def missing(self) -> list[str]:
        """The required connections that aren't set up yet."""
        return [k for k in self.required if not self.service(k).get("url")]

    def arr_instances(self, kind: str | None = None) -> list[ArrInstance]:
        """Every Sonarr/Radarr: the main ones first, then any extra instances such as a 4K Radarr."""
        out = [ArrInstance(k, k, SERVICES[k], self.raw[k]) for k in ARR_KINDS if self.service(k).get("url")]
        out += [
            ArrInstance(x["key"], x["kind"], x.get("name") or x["key"], x)
            for x in self.raw.get("extra_arrs", [])
            if x.get("url") and x.get("key")
        ]
        return [x for x in out if not kind or x.kind == kind]

    def arr_name(self, key: str) -> str:
        return next((x.name for x in self.arr_instances() if x.key == key), SERVICES.get(kind_of(key), key))

    def connections(self) -> list[tuple[str, str, JSON]]:
        """(key, name, settings) of every app Shredarr uses: Sonarr/Radarr instances, the torrent client, the media
        server, and Seerr/Prowlarr if set up."""
        out = [(x.key, x.name, x.conf) for x in self.arr_instances()]
        return out + [
            (k, SERVICES[k], self.raw[k])
            for k in (self.client_kind, self.media_kind, "seerr", "prowlarr")
            if self.service(k).get("url")
        ]

    @property
    def path_maps(self) -> list[JSON]:
        return list(self.raw.get("path_maps", []))

    # ------------------------------------------------------------------ cleanup
    @property
    def default_rule(self) -> Rule:
        r = self.raw.get("rules") or DEFAULT_RULE
        return {
            "mode": r.get("mode") or r.get("require") or "either",
            "min_seed_days": r.get("min_seed_days"),
            "min_ratio": r.get("min_ratio"),
        }

    @property
    def rule_list(self) -> list[JSON]:
        return list(self.raw.get("rule_list", []))

    @property
    def behavior(self) -> dict[str, bool]:
        return {**BEHAVIOR_DEFAULTS, **self.raw.get("behavior", {})}

    @property
    def grace_seconds(self) -> float:
        """Deletion delay: how long a torrent waits after its media is deleted before it can be removed, even once its
        seeding rule is met (time to Undo). 0 = no delay."""
        g = self.raw.get("grace", {})
        return float(g.get("value") or 0) * (86400 if g.get("unit") == "days" else 3600)

    @property
    def low_space(self) -> JSON:
        return {**LOW_SPACE_DEFAULTS, **self.raw.get("low_space", {})}

    @property
    def restore_enabled(self) -> bool:
        """Undo of RAR releases: unpack the torrent's archives with the bundled unar. On unless switched off."""
        r = self.raw.get("restore", {})
        return bool(r.get("enabled", r.get("method") != "off"))

    @property
    def save_torrents(self) -> bool:
        """Keep a copy of each torrent's .torrent file when it's deleted for good (for History → Redownload)."""
        return bool(self.raw.get("save_torrents", True))

    @property
    def torrent_retention(self) -> JSON:
        """How long saved .torrent files are kept, e.g. {"value": 1, "unit": "years"} (or days, months, forever)."""
        r = {**RETENTION_DEFAULT, **self.raw.get("torrent_retention", {})}
        return r if r["unit"] in RETENTION_UNITS else dict(RETENTION_DEFAULT)

    @property
    def torrent_retention_seconds(self) -> float | None:
        """None: kept forever."""
        r = self.torrent_retention
        unit = RETENTION_UNITS[r["unit"]]
        return None if unit is None else float(r["value"]) * unit

    @property
    def check_interval(self) -> float:
        return float(max(1, self.raw.get("check_interval_minutes", 15))) * 60

    # ------------------------------------------------------------------ sign-in, notifications
    @property
    def password_policy(self) -> JSON:
        return {**POLICY_DEFAULTS, **self.raw.get("auth_policy", {})}

    @property
    def notify(self) -> JSON:
        n = dict(self.raw.get("notify", {}))
        if n.get("ntfy_url") and not n.get("ntfy_topic"):  # older single-URL setting
            u = urllib.parse.urlparse(n["ntfy_url"])
            n["ntfy_server"], n["ntfy_topic"] = f"{u.scheme}://{u.netloc}", u.path.strip("/")
        n.pop("ntfy_url", None)
        return {**NOTIFY_DEFAULTS, **{k: v for k, v in n.items() if k in NOTIFY_DEFAULTS}}


class ConfigStore:
    """config.json is read again when it changes on disk (cheap to call often). Treat what load() returns as
    read-only; change settings with update()."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._cached: tuple[tuple[int, int, int], Config] | None = None

    def load(self) -> Config:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return Config({})
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)  # save() replaces the file, so the inode changes too
        cached = self._cached
        if cached and cached[0] == stamp:
            return cached[1]
        with open(self.path) as f:
            conf = Config(json.load(f))
        self._cached = (stamp, conf)
        return conf

    def save(self, raw: JSON) -> None:
        """Atomically, and readable only by Shredarr: the file holds API keys and passwords."""
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            # never readable by others, not even briefly
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:  # owns fd from here, so it's closed whatever happens next
                with contextlib.suppress(OSError):  # some network shares refuse chmod; the file still gets saved
                    os.fchmod(fd, 0o600)  # a .tmp left over from before keeps its old mode otherwise
                json.dump(raw, f, indent=2)
            os.replace(tmp, self.path)

    def update(self, change: Callable[[JSON], None]) -> Config:
        """Read, change and save config.json as one step."""
        with self._lock:
            raw = copy.deepcopy(self.load().raw)
            change(raw)
            self.save(raw)
            return Config(raw)
