"""Delete now (a torrent and its media, everywhere, ignoring the seeding rule) and the No torrent tab."""

from __future__ import annotations

import os
import time

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import wait_for


def plan(s, h: str) -> dict:
    return s.api("/api/plan", hash=h)


def test_delete_now_movie(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=0.1, seeding_days=1)
    keep = lab.add_movie("Sintel", 2010)
    s = start()
    assert m.torrent
    p = plan(s, m.torrent.hash)
    golden("plan", p)
    movie_id = m.id
    assert s.post("/api/delete-now", {"hash": m.torrent.hash}).status_code == 202
    s.wait_log("^  done: Delete torrent")
    assert m.torrent.hash not in lab.torrents and keep.torrent and keep.torrent.hash in lab.torrents
    assert len(lab.jellyfin.requests("DELETE")) == 1
    assert lab.radarr.requests("DELETE")[0].path == f"/api/v3/movie/{movie_id}"
    assert len(lab.seerr.requests("DELETE")) == 1
    assert not os.path.exists(m.folder) and not os.path.exists(m.torrent.disk_path)
    rows = wait_for(lambda: s.api("/api/history")["rows"], what="the History entry")  # written after the last step
    assert [(r["name"], r["reason"], r["size"]) for r in rows] == [(m.torrent.name, "Delete now", 3010)]
    wait_for(lambda: "Deleted now" in lab.ntfy.titles(), what="the notification")
    # Jellyfin then reports the delete Shredarr made itself: that webhook is ignored
    time.sleep(1.5)
    assert not any(x.startswith("Jellyfin deleted") for x in s.log())
    golden("log", s.log())


def test_delete_now_season_pack_keeps_upgraded_episodes(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 4})
    pack = lab.add_release(show, 1, range(1, 5))
    upgrade = lab.add_release(show, 1, [2], release="Paper.Moons.S01E02.2160p.WEB-DL-UHD")
    s = start()
    golden("plan", plan(s, pack.hash))
    s.post("/api/delete-now", {"hashes": [pack.hash]})
    s.wait_log("^  done: Delete torrent")
    mon = lab.sonarr.requests("PUT", "/api/v3/episode/monitor")
    assert mon[0].json() == {"episodeIds": sorted(show.episode(1, n)["id"] for n in (1, 3, 4)), "monitored": False}
    assert len(lab.jellyfin.requests("DELETE")) == 3
    assert os.path.exists(show.episode_file(1, 2)) and upgrade.hash in lab.torrents
    assert not lab.sonarr.requests("DELETE", r"/api/v3/series/\d+"), "the show stays for new episodes"
    golden("log", s.log())


def test_delete_now_cross_seed_group_and_jellyfin_elsewhere(lab: Lab, start: StartFn, golden) -> None:
    lab.jellyfin.prefix = (lab.data, "/jf")  # Jellyfin sees the library under another path: matched by folder/file
    m = lab.add_movie("Big Buck Bunny", 2008)
    assert m.torrent
    cs = lab.add_cross_seed(m.torrent)
    s = start()
    p = plan(s, cs.hash)  # from either member, the whole group goes
    golden("plan", p)
    s.post("/api/delete-now", {"hash": m.torrent.hash})
    s.wait_log("^  done: Delete cross-seed torrent")
    assert not lab.torrents
    assert len(lab.jellyfin.requests("DELETE")) == 1
    rows = wait_for(lambda: len(r := s.api("/api/history")["rows"]) == 2 and r, what="both History entries")
    assert sorted(r["cross_seed_of"] or "" for r in rows) == ["", m.torrent.name]
    golden("log", s.log())


def test_delete_now_unmanaged_torrent_rescans_jellyfin(lab: Lab, start: StartFn, golden) -> None:
    t = lab.add_unmanaged("Python.Course.2024", ["01 Intro.mp4"])
    s = start()
    golden("plan", plan(s, t.hash))
    s.post("/api/delete-now", {"hash": t.hash})
    s.wait_log("^  done: Rescan")
    assert lab.jellyfin.requests("POST", "/Library/Refresh")
    assert t.hash not in lab.torrents


def test_delete_now_dry_run(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    s = start(dry_run=True)
    assert m.torrent
    p = plan(s, m.torrent.hash)
    assert p["dry_run"] is True
    s.post("/api/delete-now", {"hash": m.torrent.hash})
    s.wait_log(r"\[dry run\] would: Delete torrent")
    assert m.torrent.hash in lab.torrents and not lab.jellyfin.requests("DELETE") and not lab.radarr.requests("DELETE")
    assert not s.api("/api/history")["rows"]
    golden("log", s.log())


def test_delete_now_unknown_hash(lab: Lab, start: StartFn, golden) -> None:
    s = start()
    golden("plan", plan(s, "0" * 40))


def test_no_torrent_tab(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("The Wire", seasons={1: 3, 2: 2})
    lab.add_library_episodes(show, 1, range(1, 4))
    lab.add_release(show, 2, [1, 2])  # still seeding: not listed
    lone = lab.add_movie("Caminandes", 2013, torrent=False)
    lab.add_movie("Sintel", 2010)
    s = start()
    o = s.api("/api/orphans")
    golden("orphans", o["items"])
    ids = [x["id"] for x in o["items"]]
    assert len(ids) == 2
    golden("plan", s.api("/api/orphan-plan", ids=",".join(ids)))
    assert s.post("/api/orphans/delete", {"ids": ids}).status_code == 202
    s.wait_log("^  done: Unmonitor")
    wait_for(lambda: len(s.api("/api/history")["rows"]) == 2, what="both History entries")
    assert not os.path.exists(lone.file)
    assert not any(os.path.exists(show.episode_file(1, n)) for n in (1, 2, 3))
    assert os.path.exists(show.episode_file(2, 1))
    rows = s.api("/api/history")["rows"]
    golden("history", [{k: r[k] for k in ("name", "title", "reason", "size", "saved_torrent")} for r in rows])
    golden("log", s.log())
    assert s.api("/api/orphans", refresh=1)["items"] == []


def test_no_torrent_counts_only_space_that_is_freed(lab: Lab, start: StartFn) -> None:
    lab.add_movie("Caminandes", 2013, torrent=False, size=3000)
    linked = lab.add_movie("Glass Half", 2015, torrent=False, size=5000)
    os.link(linked.file, os.path.join(lab.data, "elsewhere.mkv"))  # another link: deleting it frees nothing
    s = start()
    items = s.api("/api/orphans")["items"]
    assert {x["title"]: x["linked"] for x in items} == {"Caminandes": False, "Glass Half": True}
    assert s.api("/api/orphan-plan", ids=",".join(x["id"] for x in items))["size"] == 3000
