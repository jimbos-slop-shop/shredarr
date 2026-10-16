"""Undo (bring back media whose torrent is still waiting for cleanup) and Redownload (from History)."""

from __future__ import annotations

import os
import sqlite3
import time

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import Shredarr, wait_for


def undo(s: Shredarr, h: str) -> None:
    assert s.post("/api/undo", {"hash": h}).status_code == 202


def test_undo_movie(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Raygun", 2024, ratio=0.1)
    s = start()
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    assert m.torrent
    s.wait_status(m.torrent.hash, "^seeding")
    golden("plan", s.api("/api/undo-plan", hash=m.torrent.hash))
    undo(s, m.torrent.hash)
    s.wait_log("^  done: re-imported")
    wait_for(lambda: "Undo finished" in lab.ntfy.titles(), what="undo notification")
    assert os.path.exists(os.path.join(m.folder, m.torrent.name + ".mkv"))
    added = lab.radarr.requests("POST", "/api/v3/movie")[0].json()
    golden("radarr_add", {k: added[k] for k in sorted(added) if k not in ("images",)})
    assert not lab.radarr.exclusions
    scan = [c["body"] for c in lab.radarr.commands.values()]
    assert scan == [
        {
            "name": "DownloadedMoviesScan",
            "path": m.torrent.content_path,
            "downloadClientId": m.torrent.hash.upper(),
            "importMode": "Copy",
        }
    ]
    assert lab.jellyfin.requests("POST", "/Library/Media/Updated")
    assert lab.seerr.requests("POST", "/api/v1/settings/jobs/radarr-scan/run")
    assert m.torrent.hash not in s.pending()
    golden("log", s.log())


def test_undo_episodes_and_season(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 3, 2: 2})
    pack = lab.add_release(show, 1, range(1, 4))
    lab.add_release(show, 2, [1, 2])  # a Season 02 folder stays, so the show isn't treated as flat
    s = start()
    lab.delete_season_folder(show, 1)
    s.webhook(lab.season_event(show, 1))
    s.wait_status(pack.hash, "^seeding")
    assert show.record["seasons"][0]["monitored"] is False
    golden("plan", s.api("/api/undo-plan", hash=pack.hash))
    undo(s, pack.hash)
    s.wait_log("^  done: re-imported")
    assert show.record["seasons"][0]["monitored"] is True
    assert all(show.episode(1, n)["monitored"] for n in (1, 2, 3))
    assert all(show.episode(1, n)["hasFile"] for n in (1, 2, 3))
    assert len(lab.library_videos()) == 5
    golden("log", s.log())


def test_undo_series_restores_every_torrent_of_the_delete(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Harbor Lights", seasons={1: 2, 2: 2})
    t1 = lab.add_release(show, 1, [1, 2])
    t2 = lab.add_release(show, 2, [1, 2])
    show.sonarr.episode(show.id, 2, 2)["monitored"] = False
    s = start()
    lab.delete_series_folder(show)
    s.webhook(lab.series_event(show))
    s.wait_status(t2.hash, "^seeding")
    golden("plan", s.api("/api/undo-plan", hash=t1.hash))
    undo(s, t1.hash)
    s.wait_log(f"^  done: re-imported '{t2.name}'")
    s.wait_log(f"^  done: re-imported '{t1.name}'")
    assert not s.pending()
    assert [e["monitored"] for e in show.sonarr.series_episodes(show.id)] == [True, True, True, False]
    assert len(lab.library_videos()) == 4
    golden("log", s.log())


def test_undo_problems(lab: Lab, start: StartFn, golden) -> None:
    gone = lab.add_movie("Charge", 2022, ratio=0.1)
    rar = lab.add_movie("Bullet Train", 2022, rar=True, ratio=0.1)
    s = start(restore={"enabled": False})
    for m in (gone, rar):
        lab.delete_movie_files(m)
        s.webhook(lab.movie_event(m))
    assert gone.torrent and rar.torrent
    s.wait_status(rar.torrent.hash, "^seeding")
    lab.remove_torrent(gone.torrent.hash, True)
    golden("gone", s.api("/api/undo-plan", hash=gone.torrent.hash))
    golden("rar", s.api("/api/undo-plan", hash=rar.torrent.hash))
    golden("unknown", s.api("/api/undo-plan", hash="f" * 40))
    undo(s, rar.torrent.hash)
    s.wait_log("^Undo failed")
    assert s.post("/api/restore", {"enabled": True}).status_code == 200
    assert s.read_config()["restore"] == {"enabled": True}
    plan = s.api("/api/undo-plan", hash=rar.torrent.hash)
    assert plan["problem"] is None and "Unpack it again" in plan["steps"][1]


def test_redownload_same_release(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Raygun", 2024, ratio=5, seeding_days=30)
    s = start()
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("^deleted torrent")
    row = s.api("/api/history")["rows"][0]
    golden("plan", s.api("/api/redownload-plan", id=row["id"]))
    assert s.post("/api/redownload", {"id": row["id"], "how": "same"}).status_code == 202
    s.wait_log("^  done: added the original torrent back")
    assert m.torrent and m.torrent.hash in lab.torrents
    assert lab.radarr.movies and next(iter(lab.radarr.movies.values()))["monitored"] is True
    s.sweep()
    s.wait_log("^Redownload finished")
    row = s.api("/api/history")["rows"][0]
    assert row["redownload_how"] == "same" and row["redownloaded"]
    golden("log", s.log())


def test_redownload_by_search(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Raygun", 2024, ratio=5, seeding_days=30)
    show = lab.add_series("Paper Moons", seasons={1: 3, 2: 2})
    pack = lab.add_release(show, 1, range(1, 4), ratio=5, seeding_days=30)
    ep = lab.add_release(show, 2, [1], ratio=5, seeding_days=30)
    s = start(client="transmission")
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    lab.delete_series_folder(show)
    s.webhook(lab.series_event(show))
    wait_for(lambda: len(s.api("/api/history")["rows"]) == 3, what="three cleanups")
    rows = {r["name"]: r for r in s.api("/api/history")["rows"]}
    for name in (m.torrent.name if m.torrent else "", pack.name, ep.name):
        golden("plan_" + name, s.api("/api/redownload-plan", id=rows[name]["id"]))
        mark = len(s.log())
        s.post("/api/redownload", {"id": rows[name]["id"], "how": "search"})
        s.wait_log(r"^  done: asked (Radarr|Sonarr) to search", after=mark)
    cmds = sorted(
        (c["name"], c["body"].get("seasonNumber"), len(c["body"].get("episodeIds") or []))
        for x in (lab.radarr, lab.sonarr)
        for c in x.commands.values()
    )
    assert cmds == [("EpisodeSearch", None, 1), ("MoviesSearch", None, 0), ("SeasonSearch", 1, 0)]
    golden("log", sorted(s.log()[:12]) + s.log()[12:])  # the first two cleanups happen in the same sweep, in any order


def test_redownload_of_an_old_entry_is_parsed_from_the_name(lab: Lab, start: StartFn, golden) -> None:
    lab.add_series("Paper Moons", seasons={1: 3})
    lab.movie_catalog[4242] = {"title": "Wing It", "year": 2023, "imdb": "tt42"}
    s = start()

    s.sql("select 1")

    con = sqlite3.connect(s.db_path)
    con.execute(
        "insert into cleaned (ts, name, size, title, category, reason) values (?,?,?,?,?,?)",
        (time.time(), "Wing.It.2023.1080p.WEB-DL", 5, "Wing It", "radarr", "seeding rule met"),
    )
    con.execute(
        "insert into cleaned (ts, name, size, title, category, reason) values (?,?,?,?,?,?)",
        (time.time(), "Paper.Moons.S01E02.720p.HDTV", 5, "Paper Moons", "tv-sonarr", "seeding rule met"),
    )
    con.execute(
        "insert into cleaned (ts, name, size, title, category, reason) values (?,?,?,?,?,?)",
        (time.time(), "Totally Unknown Thing", 5, None, "misc", "Delete now"),
    )
    con.commit()
    con.close()
    rows = {r["name"]: r for r in s.api("/api/history")["rows"]}
    for name, r in sorted(rows.items()):
        golden("plan_" + name, s.api("/api/redownload-plan", id=r["id"]))
    s.post("/api/redownload", {"id": rows["Paper.Moons.S01E02.720p.HDTV"]["id"], "how": "search"})
    s.wait_log("^  done: asked Sonarr to search")
    assert [c["body"]["name"] for c in lab.sonarr.commands.values()] == ["EpisodeSearch"]
