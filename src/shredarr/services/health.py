"""Connection health (every app checked every few minutes) and the check for new Shredarr releases."""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from ..clients.base import TIMEOUT, describe
from ..config import JSON, ConfigStore, base_url
from .activity import Activity
from .connections import describe_service

log = logging.getLogger(__name__)


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


class Health:
    def __init__(self, config: ConfigStore, activity: Activity) -> None:
        self.config = config
        self.activity = activity
        self.status: dict[str, JSON] = {}  # service key -> {ok, msg, ts[, name]}
        self._lock = threading.Lock()

    def check(self) -> None:
        conns = self.config.load().connections()
        with self._lock:
            for k in [k for k in self.status if k not in {x[0] for x in conns}]:
                self.status.pop(k, None)
        for svc, name, conf in conns:
            was = self.status.get(svc, {}).get("ok")
            try:
                info = describe_service(svc, {**conf, "url": base_url(svc, conf["url"])})
            except Exception as e:  # whatever went wrong, the app counts as unreachable
                msg = describe(e)
                self.status[svc] = {"ok": False, "msg": msg, "ts": time.time(), "name": name}
                if was is not False:
                    self.activity.error(f"Connection FAILED: can't reach {name} ({msg})")
                continue
            self.status[svc] = {"ok": True, "msg": info, "ts": time.time()}
            if was is False:
                self.activity.info(f"Connection restored: {name}")


class Updates:
    def __init__(self, config: ConfigStore, repo: str, version: str) -> None:
        self.config = config
        self.repo = repo
        self.version = version
        self.info: JSON = {}

    def check(self) -> None:
        if not self.config.load().get("update_check", True):
            self.info.clear()
            return
        try:
            r = httpx.get(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                timeout=TIMEOUT,
                headers={"User-Agent": "Shredarr", "Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return log.warning("update check: %s", e)
        latest = (data.get("tag_name") or "").lstrip("v")
        self.info.clear()
        self.info.update(
            latest=latest, url=data.get("html_url"), newer=version_tuple(latest) > version_tuple(self.version)
        )
        return None
