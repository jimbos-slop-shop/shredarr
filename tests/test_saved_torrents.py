"""Saved .torrent files (for History → Redownload of the same release): kept for a while, then removed."""

from __future__ import annotations

import os
import time

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import Shredarr, wait_for

DAY = 86400


def saved(s: Shredarr, h: str, age_days: float) -> str:
    """A saved .torrent file of this age, as if Shredarr had kept it when deleting the torrent."""
    folder = os.path.join(s.config_dir, "torrents")
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, h + ".torrent")
    with open(p, "wb") as f:
        f.write(b"d4:infod4:name1:xee")
    os.utime(p, (time.time() - age_days * DAY,) * 2)
    return p


def test_kept_a_year_by_default_and_redownload_falls_back_to_search(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Raygun", 2024, ratio=5, seeding_days=30)
    s = start()
    assert s.api("/api/settings")["torrent_retention"] == {"value": 1, "unit": "years"}
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("^deleted torrent")
    st = s.api("/api/settings")["saved_torrents"]
    assert st["count"] == 1 and st["bytes"] > 0
    row = s.api("/api/history")["rows"][0]
    assert row["saved_torrent"] and s.api("/api/redownload-plan", id=row["id"])["same_available"]

    assert m.torrent
    os.utime(os.path.join(s.config_dir, "torrents", m.torrent.hash + ".torrent"), (time.time() - 400 * DAY,) * 2)
    assert s.post("/api/torrent-retention", {"value": "1", "unit": "years"}).status_code == 200
    assert "Removed 1 saved .torrent file older than 1 year: Redownload of those can only search again" in s.log()
    row = s.api("/api/history")["rows"][0]
    assert not row["saved_torrent"]
    plan = s.api("/api/redownload-plan", id=row["id"])
    assert plan["same_available"] is False and plan["steps"]["search"]


def test_old_files_are_removed_at_startup(lab: Lab, start: StartFn, tmp_path) -> None:
    s = Shredarr(str(tmp_path / "s"), lab.config())
    old, new = saved(s, "a" * 40, 366), saved(s, "b" * 40, 364)
    s.start()
    try:
        s.wait_log("^Removed 1 saved .torrent file older than 1 year")
        assert not os.path.exists(old) and os.path.exists(new)
    finally:
        s.stop()


def test_shorter_time_and_forever(lab: Lab, start: StartFn) -> None:
    s = start(torrent_retention={"value": 6, "unit": "months"})
    files = {d: saved(s, f"{d:040x}", d) for d in (10, 40, 400)}
    assert s.post("/api/torrent-retention", {"value": 30, "unit": "days"}).status_code == 200
    assert [d for d, p in files.items() if os.path.exists(p)] == [10]
    assert s.read_config()["torrent_retention"] == {"value": 30.0, "unit": "days"}
    os.utime(files[10], (time.time() - 1000 * DAY,) * 2)
    assert s.post("/api/torrent-retention", {"value": "", "unit": "forever"}).status_code == 200
    assert os.path.exists(files[10])
    assert s.api("/api/settings")["torrent_retention"] == {"value": 1.0, "unit": "forever"}
    log = s.log()
    assert "Saved .torrent files are kept for 30 days" in log and "Saved .torrent files are kept forever" in log


def test_delete_all_now(lab: Lab, start: StartFn) -> None:
    s = start()
    files = [saved(s, f"{i:040x}", i) for i in (1, 2)]
    wait_for(lambda: s.api("/api/settings")["saved_torrents"]["count"] == 2, what="both files counted")
    r = s.post("/api/saved-torrents/delete")
    assert r.status_code == 200 and r.text == "Deleted 2 saved .torrent files"
    assert not any(os.path.exists(p) for p in files)
    assert s.api("/api/settings")["saved_torrents"] == {"count": 0, "bytes": 0}
    assert any(m.startswith("Deleted all saved .torrent files (2 files, ") for m in s.log())


def test_invalid_settings(lab: Lab, start: StartFn) -> None:
    s = start()
    for body, msg in (
        ({"value": 1, "unit": "weeks"}, "Choose days, months, years or forever."),
        ({"value": "soon", "unit": "days"}, "How long to keep them should be a number."),
        ({"value": 0, "unit": "days"}, "Keep them for more than 0, or choose forever."),
    ):
        r = s.post("/api/torrent-retention", body)
        assert (r.status_code, r.text) == (400, msg)
    assert "torrent_retention" not in s.read_config()


def test_saving_switched_off(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Raygun", 2024, ratio=5, seeding_days=30)
    s = start()
    assert s.api("/api/settings")["save_torrents"] is True
    old, recent = saved(s, "c" * 40, 400), saved(s, "d" * 40, 10)
    assert s.post("/api/save-torrents", {"enabled": False}).status_code == 200
    assert s.read_config()["save_torrents"] is False
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("^deleted torrent")
    assert not lab.qbittorrent.requests(path="/api/v2/torrents/export"), "the torrent client isn't even asked"
    row = s.api("/api/history")["rows"][0]
    assert not row["saved_torrent"] and not s.api("/api/redownload-plan", id=row["id"])["same_available"]
    s.post("/api/torrent-retention", {"value": 1, "unit": "years"})
    assert os.path.exists(recent) and not os.path.exists(old), "files already saved stay until they expire"
    assert "Saving .torrent files for Redownload: off" in s.log()
    s.post("/api/save-torrents", {"enabled": True})
    assert s.api("/api/settings")["save_torrents"] is True
