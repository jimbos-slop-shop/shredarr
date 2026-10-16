"""The cleanup sweep: seeding rules, library checks, cross-seeds, the deletion delay and low disk space."""

from __future__ import annotations

import os

from tests.conftest import StartFn
from tests.fakes.lab import OTHER_TRACKER, Lab
from tests.harness import Shredarr, wait_for


def queue(s: Shredarr, *hashes: str) -> None:
    assert s.post("/api/queue", {"hashes": list(hashes)}).status_code == 200


def statuses(s: Shredarr, lab: Lab) -> dict[str, str]:
    names = {h: t.name for h, t in lab.torrents.items()}
    return {names.get(h, h): p["status"] for h, p in s.pending().items()}


def test_rules_decide_when_a_torrent_goes(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 3})
    other = lab.add_series("Harbor Lights", seasons={1: 1})
    t_cat = lab.add_release(other, 1, [1], ratio=1.6, seeding_days=1)  # category rule: ratio 1.5 -> met
    t_cat_title = lab.add_release(show, 1, [1], ratio=1.6, seeding_days=1)  # the title rule wins over the category rule
    t_title = lab.add_release(show, 1, [2], ratio=0, seeding_days=4, category="tv-other")  # title rule: 3 days -> met
    m_track = lab.add_movie("Elephants Dream", 2006, ratio=9, seeding_days=10, tracker=OTHER_TRACKER)  # tracker rule
    m_both = lab.add_movie("Spring", 2019, ratio=2.5, seeding_days=3)  # own rule: both 7 days and ratio 2 -> not met
    m_never = lab.add_movie("Charge", 2022, ratio=50, seeding_days=500)  # own rule: never
    m_default = lab.add_movie("Wing It", 2023, ratio=1.9, seeding_days=13.9)  # default either 14d / 2 -> not met
    m_dl = lab.add_movie("Sprite Fright", 2021, ratio=5, seeding_days=50)
    assert m_dl.torrent and m_both.torrent and m_never.torrent and m_default.torrent and m_track.torrent
    m_dl.torrent.progress = 0.5
    rules = [{"type": "category", "match": "tv-sonarr", "mode": "ratio", "min_seed_days": None, "min_ratio": 1.5},
             {"type": "title", "match": "paper moons", "mode": "days", "min_seed_days": 3, "min_ratio": None},
             {"type": "tracker", "match": "othersite.net", "mode": "both", "min_seed_days": 30, "min_ratio": 1}]
    s = start(rule_list=rules)
    for m in (m_both, m_never, m_default, m_dl, m_track):
        lab.delete_movie_files(m)
    lab.delete_series_folder(show)  # the files are gone from the library, so only the rules hold the torrents back
    lab.delete_series_folder(other)
    assert s.post("/api/rule", {"hash": m_both.torrent.hash, "mode": "both", "min_seed_days": 7, "min_ratio": 2}).status_code == 200
    assert s.post("/api/rule", {"hash": m_never.torrent.hash, "mode": "never"}).status_code == 200
    queue(s, *lab.torrents)
    s.wait_log(f"^deleted torrent '{t_title.name}'")
    s.wait_log(f"^deleted torrent '{t_cat.name}'")
    wait_for(lambda: all(p["status"] != "queued" for p in s.pending().values()), what="every torrent checked")
    assert m_track.torrent.hash in lab.torrents  # tracker rule (30 days AND ratio 1) not met
    assert t_cat_title.hash in lab.torrents
    golden("statuses", statuses(s, lab))
    golden("log", s.log())
    golden("torrents", s.api("/api/torrents")["torrents"])

    # a rule back to default
    s.post("/api/rule", {"hashes": [m_both.torrent.hash], "mode": ""})
    s.wait_log("rule for 'Spring.*back to default")
    assert not s.sql("select * from overrides where hash=?", m_both.torrent.hash)


def test_default_rule_and_dry_run_form(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Spring", 2019, ratio=1.2, seeding_days=6)
    s = start()
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    assert m.torrent
    s.wait_status(m.torrent.hash, "^seeding")
    r = s.post("/rules", data={"mode": "days", "min_seed_days": "5", "min_ratio": "", "dry_run": "on"})
    assert r.status_code == 200
    s.wait_status(m.torrent.hash, r"^\[dry run\] ready to delete")
    assert s.read_config()["rules"] == {"mode": "days", "min_seed_days": 5.0, "min_ratio": None}
    assert s.read_config()["dry_run"] is True
    r = s.post("/rules", data={"mode": "bogus", "min_seed_days": "5", "min_ratio": "1"}, fetch=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    s.wait_log(f"^deleted torrent '{m.torrent.name}'")
    golden("log", s.log())


def test_deletion_delay(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Spring", 2019, ratio=9, seeding_days=60)
    s = start(grace={"value": 2, "unit": "hours"})
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    assert m.torrent
    status = s.wait_status(m.torrent.hash, "^deletion delay")
    assert status in ("deletion delay: deletes in 1h 59m (Undo still possible); seeded 60.0 of 14 days, ratio 9.00 of 2",
                      "deletion delay: deletes in 2h 0m (Undo still possible); seeded 60.0 of 14 days, ratio 9.00 of 2")
    t = next(x for x in s.api("/api/torrents")["torrents"] if x["hash"] == m.torrent.hash)
    assert t["delete_after"] and t["undo"] == "ok"
    assert s.post("/api/grace", {"value": 0, "unit": "hours"}).status_code == 200
    s.wait_log("^deleted torrent")
    assert s.post("/api/grace", {"value": "x"}).status_code == 400


def test_torrent_removed_from_the_client_leaves_the_queue(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Spring", 2019, ratio=0.1)
    s = start()
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    assert m.torrent
    s.wait_status(m.torrent.hash, "^seeding")
    lab.remove_torrent(m.torrent.hash, True)
    s.sweep()
    s.wait_log("is gone from qBittorrent; dropping it from the queue")
    assert m.torrent.hash not in s.pending()


def test_rar_release_counts_as_in_library_through_import_history(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Bullet Train", 2022, rar=True, ratio=9, seeding_days=99)
    other = lab.add_movie("Raygun", 2024, ratio=9, seeding_days=99)
    s = start()
    assert m.torrent and other.torrent
    queue(s, m.torrent.hash)
    s.wait_status(m.torrent.hash, r"^waiting: 1 file\(s\) still in library")
    t = {x["name"]: x for x in s.api("/api/torrents")["torrents"]}
    assert t[m.torrent.name]["in_library"] == 1 and t[other.torrent.name]["in_library"] == 1
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log(f"^deleted torrent '{m.torrent.name}'")
    golden("log", s.log())


def test_leftovers_and_upgrades(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 2})
    old = lab.add_release(show, 1, [1], release="Paper.Moons.S01E01.720p.HDTV-OLD")
    new = lab.add_release(show, 1, [1], release="Paper.Moons.S01E01.1080p.WEB-NEW")  # an upgrade replaced the file
    courses = lab.add_unmanaged("Python.Course.2024", ["01 Intro.mp4", "02 Basics.mp4"])
    s = start()
    t = {x["name"]: x for x in s.api("/api/torrents")["torrents"]}
    assert t[old.name]["leftover"] is True and t[old.name]["in_library"] == 0
    assert t[new.name]["leftover"] is False and t[new.name]["in_library"] == 1
    assert t[courses.name]["tracked"] is False and t[courses.name]["leftover"] is False
    queue(s, old.hash, "not-a-hash")
    s.wait_status(old.hash, "^seeding")
    assert s.pending()[old.hash]["source"] == "leftover"
    golden("log", s.log())


def test_cross_seed_sharing_files_waits_for_its_own_rule(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=3, seeding_days=30)
    assert m.torrent
    cs = lab.add_cross_seed(m.torrent, ratio=0.4, seeding_days=2)
    s = start()
    t = {x["hash"]: x for x in s.api("/api/torrents")["torrents"]}
    assert t[cs.hash]["cross_seed_of"] == m.torrent.name and t[m.torrent.hash]["cross_seeds"] == 1
    assert t[cs.hash]["in_library"] == 1  # the same files as the library copy
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log(f"^deleted torrent '{m.torrent.name}'")
    s.wait_status(cs.hash, "^seeding")
    assert s.pending()[cs.hash]["source"] == "cross-seed"
    assert lab.qbittorrent.requests("POST", "/api/v2/torrents/delete")[-1].form()["deleteFiles"] == "true"
    assert os.path.exists(cs.disk_path), "hardlinks of the cross-seed survive"
    golden("log", s.log())


def test_cross_seed_together_with_the_original_keeps_shared_data_until_the_last(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=3, seeding_days=30)
    assert m.torrent
    cs = lab.add_cross_seed(m.torrent, same_path=True, ratio=0.1, seeding_days=1)
    s = start(behavior={"cross_seed_with_original": True})
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("cross-seed, together with the original")
    assert not lab.torrents
    assert not os.path.exists(m.torrent.disk_path)
    history = s.api("/api/history")["rows"]
    assert sorted((r["name"], r["tracker_original"]) for r in
                  [{**r, "tracker_original": r["cross_seed_of"] or ""} for r in history]) == [
        (m.torrent.name, ""), (cs.name, m.torrent.name)]
    golden("log", s.log())
    golden("history", [{k: r[k] for k in ("name", "title", "reason", "size", "cross_seed_of", "saved_torrent")} for r in history])


def test_low_disk_space_deletes_early(lab: Lab, start: StartFn, golden) -> None:
    a = lab.add_movie("Spring", 2019, ratio=0.1, seeding_days=1)
    b = lab.add_movie("Charge", 2022, ratio=0.1, seeding_days=1)
    keep = lab.add_movie("Wing It", 2023, ratio=0.1, seeding_days=1)  # still in the library
    s = start()
    for m in (a, b):
        lab.delete_movie_files(m)
        s.webhook(lab.movie_event(m))
    assert a.torrent and b.torrent and keep.torrent
    s.wait_status(b.torrent.hash, "^seeding")
    queue(s, keep.torrent.hash)
    assert s.post("/api/low-space", {"enabled": True, "min_free_gb": 10**9}).status_code == 200
    s.wait_log(f"deleting '{b.torrent.name}' early")
    wait_for(lambda: len(s.api("/api/history")["rows"]) == 2, what="both cleaned up")
    assert keep.torrent.hash in lab.torrents
    assert {r["reason"] for r in s.api("/api/history")["rows"]} == {"low disk space"}
    assert "Torrent cleaned up early" in lab.ntfy.titles()
    assert s.post("/api/low-space", {"enabled": True, "min_free_gb": "lots"}).status_code == 400
    golden("log", [x for x in s.log() if "GB free" not in x])


def test_path_mappings(lab: Lab, start: StartFn) -> None:
    lab.mount_apps_at("/data")
    m = lab.add_movie("Spring", 2019, ratio=9, seeding_days=99)
    show = lab.add_series("Paper Moons", seasons={1: 2})
    pack = lab.add_release(show, 1, [1, 2], ratio=9, seeding_days=99)
    assert m.torrent and m.torrent.content_path.startswith("/data/")
    s = start()
    t = {x["hash"]: x for x in s.api("/api/torrents")["torrents"]}
    assert t[m.torrent.hash]["in_library"] == 1 and t[pack.hash]["in_library"] == 2
    lab.delete_series_folder(show)
    s.webhook(lab.series_event(show))
    s.wait_log(f"^deleted torrent '{pack.name}'")
    assert s.post("/api/path-maps", {"maps": [{"remote": " /x ", "local": "/y"}, {"remote": "", "local": "/z"}]}).status_code == 200
    assert s.read_config()["path_maps"] == [{"remote": "/x/", "local": "/y/"}]


def test_transmission(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Spring", 2019, ratio=9, seeding_days=99)
    keep = lab.add_movie("Charge", 2022, ratio=1, seeding_days=2)
    s = start(client="transmission")
    golden("torrents", s.api("/api/torrents")["torrents"])
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("^deleted torrent")
    assert m.torrent and m.torrent.hash not in lab.torrents and keep.torrent and keep.torrent.hash in lab.torrents
    assert not os.path.exists(m.torrent.disk_path)
    assert [x["saved_torrent"] for x in s.api("/api/history")["rows"]] == [False]  # Transmission can't export .torrent files


def test_deluge(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Spring", 2019, ratio=9, seeding_days=99)
    lab.add_movie("Charge", 2022, ratio=1, seeding_days=2)
    s = start(client="deluge")
    golden("torrents", s.api("/api/torrents")["torrents"])
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("^deleted torrent")
    assert m.torrent and m.torrent.hash not in lab.torrents
    assert not os.path.exists(m.torrent.disk_path)


def test_qbittorrent_session_expiry_signs_in_again(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Spring", 2019, ratio=9, seeding_days=99)
    s = start()
    s.api("/api/torrents")
    logins = len(lab.qbittorrent.requests("POST", "/api/v2/auth/login"))
    lab.qbittorrent.expire_sessions()
    assert len(s.api("/api/torrents")["torrents"]) == 1
    assert len(lab.qbittorrent.requests("POST", "/api/v2/auth/login")) == logins + 1
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("^deleted torrent")
