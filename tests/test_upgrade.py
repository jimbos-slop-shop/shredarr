"""Upgrading keeps everything: a 1.x config.json and state.db work as they are."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3

import httpx

from tests.fakes.lab import Lab
from tests.harness import Shredarr, lan_ip

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
LAN = lan_ip()


def test_state_from_1_2_1(lab: Lab, tmp_path, golden) -> None:
    """state.db made by Shredarr 1.2.1 (tests/fixtures), with its password and session."""
    with open(os.path.join(FIXTURES, "state-1.2.1.json")) as f:
        old = json.load(f)
    waiting = lab.add_movie("Sintel", 2010, ratio=0.1, seeding_days=1)  # the torrent still waiting in that state.db
    assert waiting.torrent and waiting.torrent.hash == old["waiting"]
    lab.delete_movie_files(waiting)
    s = Shredarr(str(tmp_path / "s"), {**lab.config(), "auth": old["auth"]})
    shutil.copy(os.path.join(FIXTURES, "state-1.2.1.db"), s.db_path)
    s.start()
    try:
        rows = s.api("/api/history")["rows"]
        assert [(r["title"], r["reason"]) for r in rows] == [("Big Buck Bunny", "seeding rule met")]
        assert s.pending()[old["waiting"]]["undo"]["kind"] == "movie"
        assert s.sql("select mode, days from overrides") == [("days", 3.0)]
        assert any(m.startswith("Jellyfin deleted Movie: Sintel") for m in s.log())
        # the old session still works, and so does the password
        if LAN:  # (requests from this machine itself don't need to sign in)
            remote = f"http://{LAN}:{s.port}"
            assert httpx.get(remote + "/api/health", cookies={"shredarr_session": old["session"]}).status_code == 200
            assert httpx.get(remote + "/api/health").status_code == 401
        assert s.http.post("/login", data={"password": old["password"]}).status_code == 303
        golden("redownload_plan", s.api("/api/redownload-plan", id=rows[0]["id"]))
    finally:
        s.stop()


def test_oldest_database(lab: Lab, tmp_path) -> None:
    """A database from before per-torrent rules got their own table (they were columns of the queue)."""
    s = Shredarr(str(tmp_path / "s"), lab.config())
    con = sqlite3.connect(s.db_path)
    con.execute(
        "create table pending (hash text primary key, name text, source text, added_at real, status text, "
        "rule_mode text, rule_days real, rule_ratio real)"
    )
    con.execute("create table log (id integer primary key, ts real, msg text)")
    con.execute("insert into pending values ('a' , 'Old', 'movie', 1, 'queued', 'ratio', null, 3)")
    con.execute("insert into log (ts, msg) values (1, 'Shredarr started'), (2, '  FAILED: something')")
    con.commit()
    con.close()
    s.start()
    try:
        assert s.sql("pragma user_version") == [(3,)]
        assert s.sql("select hash, mode, days, ratio from overrides") == [("a", "ratio", None, 3.0)]
        assert {r[1] for r in s.sql("pragma table_info(pending)")} >= {"undo", "event", "content"}
        assert {r[1] for r in s.sql("pragma table_info(cleaned)")} >= {"title", "reason", "restore", "redl_how"}
        assert s.sql("select level from log where msg = '  FAILED: something'") == [(None,)]
    finally:
        s.stop()
