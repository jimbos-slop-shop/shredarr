"""Shredarr put together: every service wired up, plus the background workers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from . import __version__
from .config import JSON, ConfigStore
from .db import Database
from .repositories import ActivityRepo, HistoryRepo, OverridesRepo, PendingRepo, RedownloadsRepo, SessionsRepo
from .services.activity import Activity
from .services.auth import Auth
from .services.cleanup import Cleaner
from .services.connections import Connections
from .services.dashboard import Dashboard
from .services.delete_now import DeleteNow
from .services.deletes import MediaDeletes, Suppressions
from .services.health import Health, Updates
from .services.library import Library
from .services.notifications import Notifier
from .services.orphans import Orphans
from .services.queue import CleanupQueue
from .services.redownload import Redownloader
from .services.removal import MediaRemoval
from .services.restore import Restorer, Undo
from .services.settings_editor import SettingsEditor
from .services.trackers import Trackers
from .services.watcher import LibraryWatcher
from .services.webhook_setup import WebhookSetup
from .settings import Settings
from .text import human, plural

log = logging.getLogger(__name__)

HEALTH_EVERY = 300  # seconds
UPDATE_EVERY = 144  # health checks (12 hours)
SUMMARY_CHECK_EVERY = 30  # seconds


def spawn(fn: Callable[..., Any], *args: Any, name: str | None = None) -> threading.Thread:
    """Run fn in the background (web requests that start long jobs return right away)."""
    t = threading.Thread(target=fn, args=args, daemon=True, name=name)
    t.start()
    return t


class Shredarr:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.version = __version__
        self.config = ConfigStore(settings.config_path)
        self.db = Database(settings.db_path)
        self.pending = PendingRepo(self.db)
        self.overrides = OverridesRepo(self.db)
        self.history = HistoryRepo(self.db)
        self.redownloads = RedownloadsRepo(self.db)
        self.sessions = SessionsRepo(self.db)
        self.notifier = Notifier(self.config, settings.static_dir)
        self.activity = Activity(ActivityRepo(self.db), self.notifier)
        self.connections = Connections(self.config)
        self.library = Library(self.connections)
        self.trackers = Trackers(self.connections)
        self.suppressions = Suppressions()
        self.queue = CleanupQueue(self.pending, self.library, self.activity)
        self.deletes = MediaDeletes(
            self.config, self.connections, self.queue, self.activity, self.notifier, self.suppressions
        )
        self.cleaner = Cleaner(
            self.config,
            self.connections,
            self.library,
            self.trackers,
            self.pending,
            self.overrides,
            self.history,
            self.redownloads,
            self.activity,
            self.notifier,
            settings.torrent_dir,
        )
        self.removal = MediaRemoval(self.connections, self.suppressions)
        self.delete_now = DeleteNow(
            self.config,
            self.connections,
            self.library,
            self.removal,
            self.cleaner,
            self.pending,
            self.overrides,
            self.suppressions,
            self.activity,
            self.notifier,
        )
        self.orphans = Orphans(
            self.config,
            self.connections,
            self.library,
            self.removal,
            self.history,
            self.suppressions,
            self.activity,
            self.notifier,
        )
        self.restorer = Restorer(self.config, self.connections, self.library)
        self.undo = Undo(
            self.config, self.connections, self.library, self.restorer, self.pending, self.activity, self.notifier
        )
        self.redownloader = Redownloader(
            self.config,
            self.connections,
            self.library,
            self.restorer,
            self.history,
            self.redownloads,
            self.activity,
            self.notifier,
            settings.torrent_dir,
        )
        self.cleaner.after_sweep.append(self.redownloader.check)
        self.auth = Auth(self.config, self.sessions, settings.reset_file, self.activity, self.notifier)
        self.health = Health(self.config, self.activity)
        self.updates = Updates(self.config, settings.repo, self.version)
        self.webhook = WebhookSetup(self.config, self.connections, self.activity)
        self.dashboard = Dashboard(
            self.config,
            self.connections,
            self.library,
            self.trackers,
            self.cleaner,
            self.pending,
            self.overrides,
            self.history,
            self.health,
            self.updates,
            self.webhook,
            self.version,
            settings.repo,
            settings.torrent_dir,
        )
        self.editor = SettingsEditor(self.config, self.overrides, self.activity, lambda: spawn(self.cleaner.safe_sweep))
        self.events: queue.Queue[JSON] = queue.Queue()
        self.watcher = LibraryWatcher(
            self.library, self.suppressions, self.activity, self.events.put, settings.watch_file, settings.watch_confirm
        )

    def settings_changed(self) -> None:
        """Connections were saved: new clients, and look everything up again."""
        self.connections.reset()
        self.library.forget()
        self.trackers.forget()
        self.orphans.forget()

    def discord_logo(self) -> None:
        """With Avatar left blank, make the Shredarr logo the Discord webhook's own picture (once per webhook URL)."""
        url = self.notifier.discord_logo_wanted()
        if not url:
            return
        try:
            self.notifier.set_discord_logo(url)
            self.activity.info("Discord webhook picture set to the Shredarr logo")
        except Exception as e:  # reported; notifications still work without the picture
            self.activity.error(f"Couldn't set the Discord webhook picture (failed: {e})")

    def daily_summary(self) -> str:
        since = time.time() - 86400
        n, size = self.history.since(since)
        deletes, problems = self.activity.repo.counts_since(since)
        waiting = self.pending.count()
        media = self.config.load().media_name
        return (
            f"Last 24 hours: {plural(n, 'torrent')} cleaned up ({human(size)} freed), "
            f"{plural(deletes, 'delete')} from {media}, {plural(problems, 'problem')}.\n"
            f"Waiting for cleanup: {plural(waiting, 'torrent')}."
        )

    # ------------------------------------------------------------------ background workers
    def start(self) -> None:
        self.db.migrate()
        self.webhook.token()
        self.activity.info("Shredarr started")
        for fn in (self.event_worker, self.sweep_loop, self.summary_loop, self.health_loop, self.watch_loop):
            spawn(fn, name=fn.__name__)

    def event_worker(self) -> None:
        """Deletes reported by the media server, one at a time; a sweep once they're all handled."""
        while True:
            ev = self.events.get()
            try:
                self.deletes.handle(ev)
            except Exception as e:  # reported; the next event is still handled
                self.activity.error(f"  error: {e!r}")
            if self.events.empty():
                self.cleaner.safe_sweep()

    def sweep_loop(self) -> None:
        while True:
            self.cleaner.safe_sweep()
            time.sleep(self.config.load().check_interval)

    def summary_loop(self) -> None:
        sent = None
        while True:
            time.sleep(SUMMARY_CHECK_EVERY)
            try:
                n, today = self.config.load().notify, time.strftime("%Y-%m-%d")
                if n["daily_summary"] and time.strftime("%H:%M") == n["summary_time"] and sent != today:
                    sent = today
                    self.notifier.notify("Shredarr daily summary", self.daily_summary(), "summary")
                self.sessions.purge_expired()
            except Exception:
                log.exception("daily summary")

    def health_loop(self) -> None:
        n = 0
        while True:
            try:
                self.health.check()
                if n % UPDATE_EVERY == 0:
                    self.updates.check()
            except Exception:
                log.exception("health check")
            n += 1
            time.sleep(HEALTH_EVERY)

    def watch_loop(self) -> None:
        while True:
            try:
                c = self.config.load()
                if c.media_kind == "plex" and not c.missing:
                    self.watcher.look()
            except Exception:
                log.exception("library watch")
            time.sleep(self.settings.watch_interval)
