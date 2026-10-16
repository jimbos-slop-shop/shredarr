"""With Plex, deletes are noticed on disk (Plex can't send them), and Plex is rescanned after Shredarr's own changes."""

from __future__ import annotations

import os
import time

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import Shredarr, wait_for


def watching(s: Shredarr) -> None:
    """The first look at the library is the baseline; deletes are what disappears after it."""
    wait_for(lambda: os.path.exists(os.path.join(s.config_dir, "library-files.json")), what="first library scan")


def test_movie_deleted_on_disk(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=9, seeding_days=99)
    s = start(media="plex")
    watching(s)
    lab.delete_movie_files(m)
    s.wait_log("^deleted torrent")
    assert len(lab.radarr.requests("DELETE")) == 1 and len(lab.seerr.requests("DELETE")) == 1
    wait_for(lambda: "Deleted in Plex" in lab.ntfy.titles(), what="the notification")
    golden("log", s.log())


def test_upgrade_or_rename_is_not_a_delete(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    show = lab.add_series("Paper Moons", seasons={1: 2})
    lab.add_release(show, 1, [1, 2])
    s = start(media="plex")
    watching(s)
    os.rename(m.file, m.file.replace("Bluray-1080p", "Remux-1080p"))  # renamed: same inode
    lab.add_release(show, 1, [1], release="Paper.Moons.S01E01.2160p-UPGRADE")  # replaced by a new file

    time.sleep(4)
    assert not any("deleted" in x for x in s.log()), s.log()


def test_episode_and_season_deleted_on_disk(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 3, 2: 2})
    lab.add_release(show, 1, range(1, 4))
    lab.add_release(show, 2, [1, 2])
    s = start(media="plex")
    watching(s)
    lab.delete_episode_files(show, 1, [2])
    s.wait_log("^Plex deleted Episode")
    lab.delete_season_folder(show, 2)
    s.wait_log("^Plex deleted Season")
    s.wait_log("removed 2 episode file record\\(s\\) of 'Paper Moons' S02")
    assert show.record["seasons"][1]["monitored"] is False and show.record["seasons"][0]["monitored"] is True
    golden("log", [x for x in s.log() if not x.startswith("deleted torrent")])


def test_whole_show_deleted_on_disk(lab: Lab, start: StartFn) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 2})
    lab.add_release(show, 1, [1, 2])
    s = start(media="plex")
    watching(s)
    lab.delete_series_folder(show)
    s.wait_log("^Plex deleted Series: Paper Moons")
    s.wait_log("removed 'Paper Moons' from Sonarr")


def test_disk_going_offline_is_not_a_delete(lab: Lab, start: StartFn) -> None:
    for i in range(25):
        lab.add_movie(f"Movie {i:02d}", 2000 + i, torrent=False)
    s = start(media="plex")
    watching(s)
    for m in list(lab.movie_refs.values())[:24]:
        lab.delete_movie_files(m)
    s.wait_log("looks like a disk going offline")
    time.sleep(2)  # a few more looks at the library: the message isn't repeated on each one
    lab.delete_movie_files(list(lab.movie_refs.values())[24])  # nor when the count changes while it's still away
    time.sleep(2)
    assert sum("looks like a disk going offline" in x for x in s.log()) == 1
    assert not lab.radarr.requests("DELETE")
    wait_for(lambda: "Shredarr hit a problem" in lab.ntfy.titles(), what="a notification, in case it was a real delete")


def test_delete_now_and_undo_rescan_plex(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    u = lab.add_movie("Raygun", 2024, ratio=0.1)
    s = start(media="plex")
    watching(s)
    assert m.torrent and u.torrent
    golden("plan", s.api("/api/plan", hash=m.torrent.hash))
    s.post("/api/delete-now", {"hash": m.torrent.hash})
    s.wait_log("^  done: Rescan Plex")
    assert lab.plex.refreshes() == [("1", None)]
    assert not lab.jellyfin.requests()
    lab.delete_movie_files(u)
    s.wait_status(u.torrent.hash, "^seeding")
    lab.plex.clear_calls()
    s.post("/api/undo", {"hash": u.torrent.hash})
    s.wait_log("^  done: re-imported")
    wait_for(lab.plex.refreshes, what="a Plex scan")
    assert lab.plex.refreshes() == [("1", u.folder)]

    time.sleep(3)
    assert not any("Plex deleted Movie: Big Buck Bunny" in x for x in s.log()), "Shredarr's own delete isn't reported"
    golden("log", [x for x in s.log() if "Raygun" not in x or "Undo" in x])
