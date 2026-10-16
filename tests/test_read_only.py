"""Read-only mode (SHREDARR_READ_ONLY), for a test copy next to the real one: nothing is ever changed in another app."""

from __future__ import annotations

import os
import sqlite3
import time

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import wait_for

QBIT_WRITES = ("/api/v2/torrents/delete", "/api/v2/torrents/add")


def changes(lab: Lab) -> list[tuple[str, str, str]]:
    """Every request that would have changed something in an app."""
    out = [
        (f.name, r.method, r.path)
        for f in (lab.sonarr, lab.radarr, lab.seerr, lab.jellyfin, lab.prowlarr)
        for r in f.calls
        if r.method != "GET"
    ]
    out += [("qbittorrent", r.method, r.path) for r in lab.qbittorrent.calls if r.path in QBIT_WRITES]
    out += [("plex", r.method, r.path) for r in lab.plex.calls if r.path.endswith("/refresh")]
    return out + [("ntfy", r.method, r.path) for r in lab.ntfy.calls]


def test_a_read_only_copy_changes_nothing(lab: Lab, start: StartFn) -> None:
    gone = lab.add_movie("Raygun", 2024, ratio=9, seeding_days=99)  # seeding rule met
    other = lab.add_movie("Sintel", 2010)
    assert gone.torrent and other.torrent
    s = start(env={"SHREDARR_READ_ONLY": "1"})
    assert s.log()[0] == "Shredarr started read-only: it won't change anything in your apps"
    page = s.get("/").text
    assert "<title>Shredarr (test)</title>" in page and "Test copy, read-only." in page
    assert s.api("/api/torrents")["read_only"] is True

    # a delete reported by Jellyfin: Radarr and Seerr are left alone, and so is the torrent
    lab.delete_movie_files(gone)
    s.webhook(lab.movie_event(gone))
    s.wait_log(r"^  FAILED: removed 'Raygun' from Radarr .*: read-only test copy, so nothing was changed in Radarr$")
    s.wait_status(gone.torrent.hash, "^error: read-only test copy, so nothing was changed in qBittorrent$")

    # Delete now, Undo and Redownload
    s.post("/api/delete-now", {"hash": other.torrent.hash})
    s.wait_log(r"^  FAILED: Delete torrent 'Sintel.*nothing was changed in qBittorrent$")
    mark = len(s.log())
    s.post("/api/undo", {"hash": gone.torrent.hash})
    s.wait_log("^  FAILED: read-only test copy, so nothing was changed in Radarr$", after=mark)
    con = sqlite3.connect(s.db_path)
    con.execute(
        "insert into cleaned (ts, name, size, title, category, reason) values (?,?,?,?,?,?)",
        (time.time(), "Spring.2019.1080p.BluRay.x264-LAB", 5, "Spring", "radarr", "seeding rule met"),
    )
    con.commit()
    con.close()
    lab.movie_catalog[4242] = {"title": "Spring", "year": 2019, "imdb": "tt42"}
    row = s.api("/api/history")["rows"][0]
    s.post("/api/redownload", {"id": row["id"], "how": "search"})
    s.wait_log("^  FAILED: redownload of Spring: read-only test copy, so nothing was changed in Radarr$")

    # settings that would reach into other apps
    r = s.post("/api/jellyfin-webhook", {"url": "http://shredtest:8089"})
    assert r.status_code == 400 and r.text.endswith("read-only test copy, so nothing was changed in Jellyfin")
    r = s.post("/api/notify-test")
    assert (r.status_code, r.text) == (
        400,
        "Read-only test copy, so nothing was changed in your notification services.",
    )

    time.sleep(1)
    assert changes(lab) == []
    assert gone.torrent.hash in lab.torrents and other.torrent.hash in lab.torrents
    assert os.path.exists(other.file)
    assert lab.radarr.movies and not lab.radarr.exclusions


def test_plex_scans_are_not_sent(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    s = start(media="plex", env={"SHREDARR_READ_ONLY": "1"})
    assert m.torrent
    s.post("/api/delete-now", {"hash": m.torrent.hash})
    s.wait_log("^  done: Rescan Plex")  # rescans are best effort: a refused one is only a warning in the container log
    assert "nothing was changed in Plex" in wait_for(s.output, what="the warning")
    assert changes(lab) == []
