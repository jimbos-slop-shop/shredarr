"""One class per state.db table, so SQL stays in one place."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .config import JSON, Rule
from .db import Database

ACTIVITY_KEEP = 2000  # activity log lines kept


@dataclass
class Pending:
    """A torrent waiting for cleanup."""

    hash: str
    name: str  # what was deleted, e.g. "Big Buck Bunny" or "'Paper Moons' S01E05"
    source: str  # movie, series, season, episode, cross-seed, leftover
    added_at: float
    status: str | None
    undo: JSON | None  # what Undo needs to bring the media back
    event: str | None  # torrents queued by the same delete share this (a whole show is undone as one)


class PendingRepo:
    COLUMNS = "hash, name, source, added_at, status, undo, event"

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(r: tuple[Any, ...]) -> Pending:
        return Pending(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]) if r[5] else None, r[6])

    def all(self) -> list[Pending]:
        """Oldest first."""
        with self.db.connect() as con:
            return [self._row(r) for r in con.execute(f"select {self.COLUMNS} from pending order by added_at")]

    def get(self, h: str) -> Pending | None:
        with self.db.connect() as con:
            r = con.execute(f"select {self.COLUMNS} from pending where hash=?", (h,)).fetchone()
        return self._row(r) if r else None

    def by_event(self, event: str, exclude: str) -> list[Pending]:
        with self.db.connect() as con:
            return [
                self._row(r)
                for r in con.execute(f"select {self.COLUMNS} from pending where event=? and hash!=?", (event, exclude))
            ]

    def content(self, h: str) -> JSON | None:
        with self.db.connect() as con:
            r = con.execute("select content from pending where hash=?", (h,)).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    def add(
        self,
        h: str,
        name: str,
        source: str,
        *,
        undo: JSON | None = None,
        event: str | None = None,
        content: JSON | None = None,
    ) -> bool:
        """Queue a torrent. Returns False if it was already queued (nothing changes then)."""
        with self.db.connect() as con:
            cur = con.execute(
                "insert or ignore into pending (hash, name, source, added_at, status, undo, event, content) "
                "values (?,?,?,?,?,?,?,?)",
                (
                    h,
                    name,
                    source,
                    time.time(),
                    "queued",
                    json.dumps(undo) if undo else None,
                    event,
                    json.dumps(content) if content is not None else None,
                ),
            )
            return cur.rowcount > 0

    def set_undo(self, h: str, undo: JSON) -> None:
        with self.db.connect() as con:
            con.execute("update pending set undo=? where hash=?", (json.dumps(undo), h))

    def set_status(self, h: str, status: str) -> None:
        with self.db.connect() as con:
            con.execute("update pending set status=? where hash=?", (status, h))

    def remove(self, *hashes: str, source: str | None = None) -> None:
        with self.db.connect() as con:
            if source:
                con.executemany("delete from pending where hash=? and source=?", [(h, source) for h in hashes])
            else:
                con.executemany("delete from pending where hash=?", [(h,) for h in hashes])

    def count(self) -> int:
        with self.db.connect() as con:
            return int(con.execute("select count(*) from pending").fetchone()[0])


class OverridesRepo:
    """Per-torrent seeding rules. Torrents without one follow the rules in Settings."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def all(self) -> dict[str, Rule]:
        with self.db.connect() as con:
            return {
                h: {"mode": m, "min_seed_days": d, "min_ratio": r}
                for h, m, d, r in con.execute("select hash, mode, days, ratio from overrides")
            }

    def set(self, h: str, mode: str, days: float | None, ratio: float | None) -> None:
        with self.db.connect() as con:
            con.execute("insert or replace into overrides values (?,?,?,?)", (h, mode, days, ratio))

    def remove(self, *hashes: str) -> None:
        with self.db.connect() as con:
            con.executemany("delete from overrides where hash=?", [(h,) for h in hashes])


class SessionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, token: str, days: int) -> None:
        with self.db.connect() as con:
            con.execute("insert into sessions values (?,?,?)", (token, time.time(), time.time() + days * 86400))

    def valid(self, token: str) -> bool:
        with self.db.connect() as con:
            row = con.execute("select expires from sessions where token=?", (token,)).fetchone()
        return bool(row) and row[0] > time.time()

    def remove(self, token: str | None) -> None:
        with self.db.connect() as con:
            con.execute("delete from sessions where token=?", (token,))

    def remove_all(self, keep: str | None = None) -> None:
        with self.db.connect() as con:
            con.execute("delete from sessions where token != ?", (keep or "",))

    def purge_expired(self) -> None:
        with self.db.connect() as con:
            con.execute("delete from sessions where expires < ?", (time.time(),))


@dataclass
class Cleaned:
    """A History entry: a torrent (or media without one) that Shredarr deleted."""

    id: int
    ts: float
    name: str
    size: int | None
    title: str | None
    category: str | None
    reason: str | None
    hash: str | None
    restore: JSON | None  # what Redownload needs
    redownloaded: float | None
    redownload_how: str | None


class HistoryRepo:
    COLUMNS = "rowid, ts, name, size, title, category, reason, hash, restore, redl_ts, redl_how"

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(r: tuple[Any, ...]) -> Cleaned:
        return Cleaned(
            r[0],
            r[1],
            r[2],
            r[3],
            r[4],
            r[5],
            r[6],
            r[7],
            json.loads(r[8]) if r[8] else None,
            float(r[9]) if r[9] else None,
            r[10],
        )

    def add(
        self,
        name: str,
        size: int,
        *,
        title: str | None,
        category: str | None,
        reason: str,
        h: str | None,
        restore: JSON | None,
    ) -> None:
        with self.db.connect() as con:
            con.execute(
                "insert into cleaned (ts, name, size, title, category, reason, hash, restore) values (?,?,?,?,?,?,?,?)",
                (time.time(), name, size, title, category, reason, h, json.dumps(restore) if restore else None),
            )

    def recent(self, limit: int = 1000) -> list[Cleaned]:
        with self.db.connect() as con:
            return [
                self._row(r)
                for r in con.execute(f"select {self.COLUMNS} from cleaned order by ts desc limit ?", (limit,))
            ]

    def get(self, row_id: int) -> Cleaned | None:
        with self.db.connect() as con:
            r = con.execute(f"select {self.COLUMNS} from cleaned where rowid=?", (row_id,)).fetchone()
        return self._row(r) if r else None

    def mark_redownloaded(self, row_id: int, how: str) -> None:
        with self.db.connect() as con:
            con.execute("update cleaned set redl_ts=?, redl_how=? where rowid=?", (time.time(), how, row_id))

    def totals(self) -> tuple[int, int, int]:
        """(bytes freed ever, cleanups ever, bytes freed in the last 30 days)."""
        with self.db.connect() as con:
            size, count = con.execute("select coalesce(sum(size), 0), count(*) from cleaned").fetchone()
            last30 = con.execute(
                "select coalesce(sum(size), 0) from cleaned where ts > ?", (time.time() - 30 * 86400,)
            ).fetchone()[0]
        return int(size), int(count), int(last30)

    def since(self, ts: float) -> tuple[int, int]:
        """(cleanups, bytes freed) since ts."""
        with self.db.connect() as con:
            n, size = con.execute("select count(*), coalesce(sum(size), 0) from cleaned where ts > ?", (ts,)).fetchone()
        return int(n), int(size)


@dataclass
class Redownload:
    hash: str
    ts: float
    app: str
    title: str
    tries: int
    last_try: float | None


class RedownloadsRepo:
    """Torrents added back from History, watched until Sonarr/Radarr has imported them."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, h: str, app: str, title: str) -> None:
        with self.db.connect() as con:
            con.execute(
                "insert or replace into redownloads (hash, ts, app, title, tries, last_try, done) "
                "values (?,?,?,?,0,0,0)",
                (h, time.time(), app, title),
            )

    def open(self) -> list[Redownload]:
        with self.db.connect() as con:
            return [
                Redownload(*r)
                for r in con.execute("select hash, ts, app, title, tries, last_try from redownloads where done=0")
            ]

    def done(self, h: str) -> None:
        with self.db.connect() as con:
            con.execute("update redownloads set done=1 where hash=?", (h,))

    def tried(self, h: str) -> None:
        with self.db.connect() as con:
            con.execute("update redownloads set tries=tries+1, last_try=? where hash=?", (time.time(), h))


class ActivityRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, msg: str, level: str) -> None:
        with self.db.connect() as con:
            con.execute("insert into log (ts, msg, level) values (?, ?, ?)", (time.time(), msg, level))
            con.execute("delete from log where id <= (select max(id) from log) - ?", (ACTIVITY_KEEP,))

    def recent(self, limit: int = 500) -> list[tuple[float, str]]:
        """Newest first."""
        with self.db.connect() as con:
            return [(ts, msg) for ts, msg in con.execute("select ts, msg from log order by id desc limit ?", (limit,))]

    def counts_since(self, ts: float) -> tuple[int, int]:
        """(deletes reported by the media server, problems) since ts."""
        with self.db.connect() as con:
            deletes = con.execute(
                "select count(*) from log where ts > ? and (msg like 'Jellyfin deleted%' or msg like 'Plex deleted%')",
                (ts,),
            ).fetchone()[0]
            # lines from before problems were marked are recognized by their wording, as they used to be
            problems = con.execute(
                "select count(*) from log where ts > ? and (level = 'error' or (level is null and "
                "(msg like '%FAILED%' or msg like '%failed%')))",
                (ts,),
            ).fetchone()[0]
        return int(deletes), int(problems)
