"""Notifications to Discord, ntfy and Pushover."""

from __future__ import annotations

import base64
import logging
import os
import threading

import httpx

from ..clients.base import TIMEOUT
from ..config import JSON, ConfigStore

log = logging.getLogger(__name__)

SWITCHES = {
    "cleanup": "on_cleanup",
    "failure": "on_failure",
    "jellyfin": "on_jellyfin",
    "undo": "on_undo",
    "summary": "daily_summary",
}
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
HEADERS = {"User-Agent": "Shredarr"}


class Notifier:
    def __init__(self, config: ConfigStore, static_dir: str) -> None:
        self.config = config
        self.static_dir = static_dir

    def notify(self, title: str, msg: str, kind: str) -> None:
        """Send in the background, if the switch for this kind of event is on (kinds without a switch always send)."""
        try:
            n = self.config.load().notify
        except (OSError, ValueError):
            return
        if kind in SWITCHES and not n.get(SWITCHES[kind]):
            return
        self.send_in_background(n, title, msg)

    def send_in_background(self, n: JSON, title: str, msg: str) -> None:
        threading.Thread(target=self.send, args=(n, title, msg), daemon=True, name="notify").start()

    def send(self, n: JSON, title: str, msg: str) -> tuple[list[str], list[str]]:
        """Returns (services tried, errors). Errors are only printed, never put in the activity log (whose problems are
        notified: a broken service would loop)."""
        tried: list[str] = []
        errs: list[str] = []
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS) as http:

            def attempt(name: str, request: httpx.Request) -> None:
                tried.append(name)
                try:
                    r = http.send(request)
                    if r.status_code >= 400:
                        errs.append(f"{name}: HTTP {r.status_code} {r.text[:200]}")
                except httpx.HTTPError as e:
                    errs.append(f"{name}: {e}")

            if n["discord_url"]:
                payload: JSON = {"embeds": [{"title": title, "description": msg[:4000], "color": 0x3B82F6}]}
                if n["discord_username"]:
                    payload["username"] = n["discord_username"]
                if n["discord_avatar"]:
                    payload["avatar_url"] = n["discord_avatar"]
                attempt("Discord", http.build_request("POST", n["discord_url"], json=payload))
            if n["ntfy_topic"]:
                headers = {"Authorization": "Bearer " + n["ntfy_token"]} if n["ntfy_token"] else {}
                attempt(
                    "ntfy",
                    http.build_request(
                        "POST",
                        (n["ntfy_server"] or "https://ntfy.sh").rstrip("/"),
                        headers=headers,
                        json={
                            "topic": n["ntfy_topic"],
                            "title": title,
                            "message": msg,
                            "tags": ["wastebasket"],
                            "priority": int(n["ntfy_priority"] or 3),
                        },
                    ),
                )
            if n["pushover_user"] and n["pushover_token"]:
                form = {
                    "token": n["pushover_token"],
                    "user": n["pushover_user"],
                    "title": title,
                    "message": msg[:1024],
                    "priority": n["pushover_priority"] or "0",
                }
                if n["pushover_sound"]:
                    form["sound"] = n["pushover_sound"]
                if form["priority"] == "2":  # emergency priority requires retry/expire
                    form.update(retry="60", expire="3600")
                attempt("Pushover", http.build_request("POST", PUSHOVER_URL, data=form))
        for e in errs:
            log.warning("[notify] %s", e)
        return tried, errs

    def discord_logo_wanted(self) -> str | None:
        """The Discord webhook URL that should get the Shredarr logo as its picture: Avatar is left blank and this
        webhook doesn't have it yet."""
        n, c = self.config.load().notify, self.config.load()
        if not n["discord_url"] or n["discord_avatar"] or c.get("discord_logo_set") == n["discord_url"]:
            return None
        return str(n["discord_url"])

    def set_discord_logo(self, url: str) -> None:
        """Discord can't fetch images from this private server, so the logo is uploaded to the webhook instead of
        being linked."""
        with open(os.path.join(self.static_dir, "logo.png"), "rb") as f:
            data = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        r = httpx.patch(url, json={"avatar": data}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        self.config.update(lambda raw: raw.update(discord_logo_set=url))
