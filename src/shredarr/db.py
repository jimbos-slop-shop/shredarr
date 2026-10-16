"""state.db: the cleanup queue, per-torrent rules, History, the activity log and sign-in sessions.

The schema is versioned with SQLite's user_version. Version 1 is the schema Shredarr 1.x built up over time (its
steps are safe to run on any 1.x database); every later change is a new step in MIGRATIONS.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager


def _v1_shredarr_1x(con: sqlite3.Connection) -> None:
    con.execute(
        "create table if not exists pending (hash text primary key, name text, source text, added_at real, status text)"
    )
    con.execute("create table if not exists log (id integer primary key, ts real, msg text)")
    have = {r[1] for r in con.execute("pragma table_info(pending)")}
    for col, typ in (
        ("rule_mode", "text"),
        ("rule_days", "real"),
        ("rule_ratio", "real"),
        ("undo", "text"),
        ("event", "text"),
        ("content", "text"),
    ):
        if col not in have:
            con.execute(f"alter table pending add column {col} {typ}")
    # per-torrent seeding rules used to live in pending; any torrent without a row follows the default rule
    con.execute("create table if not exists overrides (hash text primary key, mode text, days real, ratio real)")
    con.execute(
        "insert or ignore into overrides select hash, rule_mode, rule_days, rule_ratio from pending "
        "where rule_mode is not null"
    )
    con.execute("update pending set rule_mode = null where rule_mode is not null")
    con.execute("create table if not exists sessions (token text primary key, created real, expires real)")
    con.execute(
        "create table if not exists redownloads (hash text primary key, ts real, app text, title text, "
        "tries integer default 0, last_try real, done integer default 0)"
    )
    con.execute("create table if not exists cleaned (ts real, name text, size integer)")
    have = {r[1] for r in con.execute("pragma table_info(cleaned)")}
    for col in ("title", "category", "reason", "hash", "restore", "redl_ts", "redl_how"):
        if col not in have:
            con.execute(f"alter table cleaned add column {col} text")


def _v2_log_level(con: sqlite3.Connection) -> None:
    """Activity log lines that report a problem are marked as such (they used to be recognized by their wording)."""
    if "level" not in {r[1] for r in con.execute("pragma table_info(log)")}:
        con.execute("alter table log add column level text")


def _v3_history_index(con: sqlite3.Connection) -> None:
    """History is read newest first and summed by date on every refresh, and it keeps growing."""
    con.execute("create index if not exists cleaned_ts on cleaned(ts)")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_v1_shredarr_1x, _v2_log_level, _v3_history_index]


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._migrated = False
        self._lock = threading.Lock()

    def migrate(self) -> None:
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30)
            try:
                version = con.execute("pragma user_version").fetchone()[0]
                for i, step in enumerate(MIGRATIONS[version:], start=version + 1):
                    with con:
                        # sqlite3 only opens a transaction by itself before inserts and updates, not before
                        # "alter table": begin one, so a step and its version number are saved together or not at all
                        con.execute("begin")
                        step(con)
                        con.execute(f"pragma user_version = {i}")
            finally:
                con.close()
            self._migrated = True

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection for one unit of work: committed at the end (rolled back on an error), then closed."""
        if not self._migrated:
            self.migrate()
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()
