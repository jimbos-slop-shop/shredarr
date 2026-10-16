"""The activity log: what Shredarr did, shown on the dashboard and printed to the container log."""

from __future__ import annotations

import logging

from ..repositories import ActivityRepo
from .notifications import Notifier

log = logging.getLogger("shredarr.activity")


class Activity:
    def __init__(self, repo: ActivityRepo, notifier: Notifier) -> None:
        self.repo = repo
        self.notifier = notifier

    def info(self, msg: str) -> None:
        log.info(msg)
        self.repo.add(msg, "info")

    def error(self, msg: str) -> None:
        """Something went wrong: also sent as a notification (if those are switched on)."""
        log.warning(msg)
        self.repo.add(msg, "error")
        self.notifier.notify("Shredarr hit a problem", msg.strip(), "failure")

    def recent(self, limit: int = 500) -> list[tuple[float, str]]:
        return self.repo.recent(limit)
