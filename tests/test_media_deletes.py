"""Deletes reported by Jellyfin's webhook: Sonarr, Radarr and Seerr are cleaned up right away, the torrent is queued."""

from __future__ import annotations

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import wait_for


def test_movie_delete_cleans_up_everywhere_then_the_torrent(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=3.1, seeding_days=20)
    keep = lab.add_movie("Sintel", 2010, ratio=0.2, seeding_days=1)
    s = start()
    movie_id = m.id
    lab.delete_movie_files(m)
    assert s.webhook(lab.movie_event(m)).status_code == 200

    s.wait_log(r"^deleted torrent")
    assert m.torrent and m.torrent.hash not in lab.torrents
    assert keep.torrent and keep.torrent.hash in lab.torrents
    assert not any("Big.Buck.Bunny" in p for p in lab.library_videos())

    delete = lab.radarr.requests("DELETE", r"/api/v3/movie/\d+")
    assert [(r.path, r.query) for r in delete] == [
        (f"/api/v3/movie/{movie_id}", {"deleteFiles": "true", "addImportExclusion": "true"})]
    assert [e["tmdbId"] for e in lab.radarr.exclusions] == [m.tmdb]
    assert len(lab.seerr.requests("DELETE")) == 1
    form = lab.qbittorrent.requests("POST", "/api/v2/torrents/delete")[-1].form()
    assert form["hashes"] == m.torrent.hash and form["deleteFiles"].lower() == "true"
    assert "Torrent cleaned up" in lab.ntfy.titles()
    assert "Deleted in Jellyfin" in lab.ntfy.titles()

    history = s.api("/api/history")
    assert [(r["name"], r["title"], r["reason"]) for r in history["rows"]] == [
        (m.torrent.name, "Big Buck Bunny", "seeding rule met")]
    assert history["rows"][0]["size"] == 3010  # video + .nfo of the download; the library link was already gone
    assert history["rows"][0]["saved_torrent"] is True
    golden("log", s.log())


def test_movie_still_on_disk_is_left_alone(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=3.1, seeding_days=20)
    s = start()
    s.webhook(lab.movie_event(m))
    s.wait_log("still has its file")
    assert not lab.radarr.requests("DELETE")
    assert not lab.seerr.requests("DELETE")
    assert m.torrent and m.torrent.hash in lab.torrents


def test_movie_matched_by_imdb_then_by_title(lab: Lab, start: StartFn) -> None:
    a = lab.add_movie("Cosmos Laundromat", 2015, ratio=0.1)
    b = lab.add_movie("Spring", 2019, ratio=0.1)
    s = start()
    for m in (a, b):
        lab.delete_movie_files(m)
    s.webhook({**lab.movie_event(a), "tmdb_id": None})
    s.webhook({**lab.movie_event(b), "tmdb_id": None, "imdb_id": None})
    s.wait_log("removed 'Spring' from Radarr")
    assert len(lab.radarr.requests("DELETE")) == 2
    assert {p["name"] for p in s.pending().values()} == {"Cosmos Laundromat", "Spring"}


def test_movie_not_in_radarr(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Agent 327", 2017)
    s = start()
    s.webhook({**lab.movie_event(m), "tmdb_id": 1, "imdb_id": "tt0", "name": "Something Else"})
    s.wait_log("no Radarr match")
    assert not lab.radarr.requests("DELETE")
    golden("log", s.log())


def test_series_delete_removes_the_show(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Harbor Lights", seasons={1: 4, 2: 3})
    s1 = lab.add_release(show, 1, range(1, 5), ratio=5, seeding_days=30)
    s2 = lab.add_release(show, 2, range(1, 4), ratio=0.5, seeding_days=2)
    s = start()
    sid = show.id
    lab.delete_series_folder(show)
    s.webhook(lab.series_event(show))
    s.wait_log(f"^deleted torrent '{s1.name}'")
    s.wait_status(s2.hash, "^seeding")
    assert [(r.path, r.query) for r in lab.sonarr.requests("DELETE")] == [
        (f"/api/v3/series/{sid}", {"deleteFiles": "true", "addImportListExclusion": "true"})]
    assert [e["tvdbId"] for e in lab.sonarr.exclusions] == [show.tvdb]
    assert len(lab.seerr.requests("DELETE")) == 1
    p = s.pending()[s2.hash]
    assert p["source"] == "series" and p["undo"]["kind"] == "series"
    assert p["undo"]["seasons"] == [[1, True], [2, True]]
    assert len(p["undo"]["monitored_eps"]) == 7
    assert p["content"]["episodes"] == [[2, 1], [2, 2], [2, 3]]
    golden("log", s.log())
    golden("pending", sorted((v["name"], v["source"], v["status"]) for v in s.pending().values()))


def test_series_event_with_folder_still_there_keeps_the_show(lab: Lab, start: StartFn) -> None:
    show = lab.add_series("Harbor Lights")
    lab.add_release(show, 1, range(1, 7))
    s = start()
    s.webhook(lab.series_event(show))
    s.wait_log("just an empty show being dropped")
    assert not lab.sonarr.requests("DELETE")
    assert not lab.seerr.requests("DELETE")


def test_season_delete(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("The Night Garden", seasons={1: 3, 2: 3})
    pack = lab.add_release(show, 1, range(1, 4), ratio=2.5, seeding_days=3)
    lab.add_release(show, 2, range(1, 4))
    s = start()
    lab.delete_season_folder(show, 1)
    s.webhook(lab.season_event(show, 1))
    s.wait_log(f"^deleted torrent '{pack.name}'")
    s1_ids = sorted(show.episode(1, n)["id"] for n in (1, 2, 3))
    mon = lab.sonarr.requests("PUT", "/api/v3/episode/monitor")
    assert [r.json() for r in mon] == [{"episodeIds": s1_ids, "monitored": False}]
    assert [x["monitored"] for x in show.record["seasons"]] == [False, True]
    bulk = lab.sonarr.requests("DELETE", "/api/v3/episodefile/bulk")
    assert len(bulk) == 1 and len(bulk[0].json()["episodeFileIds"]) == 3
    assert not lab.seerr.requests("DELETE")
    assert not lab.sonarr.requests("DELETE", r"/api/v3/series/\d+")
    golden("log", s.log())


def test_emptied_season_folder_keeps_the_season(lab: Lab, start: StartFn) -> None:
    show = lab.add_series("The Night Garden", seasons={1: 3})
    lab.add_release(show, 1, range(1, 4))
    s = start()
    lab.delete_episode_files(show, 1, range(1, 4))  # all episodes gone, but the Season 01 folder stays
    s.webhook(lab.season_event(show, 1))
    s.wait_log("just dropped an empty season")
    assert not lab.sonarr.requests("PUT")


def test_episode_delete_from_season_pack_waits_for_the_rest(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 6})
    pack = lab.add_release(show, 1, range(1, 7), ratio=9, seeding_days=99)
    s = start()
    lab.delete_episode_files(show, 1, [5])
    s.webhook(lab.episode_event(show, 1, 5))
    status = s.wait_status(pack.hash, "^waiting")
    assert status.startswith("waiting: 5 file(s) still in library")
    assert [r.json() for r in lab.sonarr.requests("PUT", "/api/v3/episode/monitor")] == [
        {"episodeIds": [show.episode(1, 5)["id"]], "monitored": False}]
    assert len(lab.sonarr.requests("DELETE", "/api/v3/episodefile/bulk")) == 1
    assert show.record["seasons"][0]["monitored"] is True
    p = s.pending()[pack.hash]
    assert p["source"] == "episode" and p["undo"] == {
        "kind": "episodes", "app": "sonarr", "series_id": show.id, "title": "Paper Moons",
        "remonitor": [show.episode(1, 5)["id"]], "seasons": []}
    golden("log", s.log())

    # a second episode of the same pack: its undo info is merged
    lab.delete_episode_files(show, 1, [6])
    s.webhook(lab.episode_event(show, 1, 6))
    s.wait_status(pack.hash, "^waiting: 4 file")
    assert s.pending()[pack.hash]["undo"]["remonitor"] == sorted([show.episode(1, 5)["id"], show.episode(1, 6)["id"]])


def test_multi_episode_file(lab: Lab, start: StartFn) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 6})
    lab.add_release(show, 1, [1], ratio=0.1)
    lab.add_release(show, 1, [2], ratio=0.1)
    lab.add_release(show, 1, [3], ratio=0.1)
    s = start()
    lab.delete_episode_files(show, 1, [1, 2])
    ev = {**lab.episode_event(show, 1, 1, end=2), "tvdb_id": None}  # matched by episode numbers
    s.webhook(ev)
    s.wait_log("removed 2 episode file record")
    assert lab.sonarr.requests("PUT", "/api/v3/episode/monitor")[0].json()["episodeIds"] == sorted(
        show.episode(1, n)["id"] for n in (1, 2))
    assert len(s.pending()) == 2


def test_flat_show_season_only_unmonitors_aired_episodes(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("What We Do", seasons={6: 4}, flat=True, unaired=((6, 4),))
    lab.add_release(show, 6, range(1, 4))
    s = start()
    lab.delete_episode_files(show, 6, range(1, 4))
    s.webhook(lab.season_event(show, 6))
    s.wait_log("removed 3 episode file record")
    assert lab.sonarr.requests("PUT", "/api/v3/episode/monitor")[0].json()["episodeIds"] == sorted(
        show.episode(6, n)["id"] for n in (1, 2, 3))
    assert not lab.sonarr.requests("PUT", r"/api/v3/series/\d+")
    assert show.record["seasons"][0]["monitored"] is True
    golden("log", s.log())


def test_unknown_show_and_ignored_items(lab: Lab, start: StartFn, golden) -> None:
    s = start()
    s.webhook(lab.event("Episode", series_name="Nope", season_number=1, episode_number=1, item_id="x"))
    s.webhook(lab.event("Audio", name="A song"))
    s.webhook(lab.event("Season", series_name="Nope"))  # no season number
    s.wait_log("ignored")
    log = s.log()
    assert "  no Sonarr match for the series" in log
    golden("log", log)


def test_webhook_needs_the_token_and_json(lab: Lab, start: StartFn) -> None:
    s = start()
    assert s.webhook(lab.event("Movie"), token="wrong").status_code == 401
    assert s.webhook(lab.event("Movie"), token=None).status_code == 401
    r = s.http.post("/webhook/jellyfin", content=b"{nope", headers={"X-Webhook-Token": "lab-webhook-token"})
    assert r.status_code == 400


def test_dry_run_changes_nothing(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=3.1, seeding_days=20)
    show = lab.add_series("Paper Moons", seasons={1: 2})
    lab.add_release(show, 1, [1], ratio=5)
    s = start(dry_run=True)
    lab.delete_movie_files(m)
    lab.delete_episode_files(show, 1, [1])
    s.webhook(lab.movie_event(m))
    s.webhook(lab.episode_event(show, 1, 1))
    s.wait_log(r"\[dry run\] would have deleted torrent '.*S01E01")
    assert not any(f.requests("DELETE") or f.requests("PUT") for f in lab.fakes)
    assert not lab.qbittorrent.requests("POST", "/api/v2/torrents/delete")
    assert len(lab.torrents) == 2
    golden("log", s.log())


def test_cleanup_behavior_switches(lab: Lab, start: StartFn) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    show = lab.add_series("Paper Moons", seasons={1: 2})
    lab.add_release(show, 1, [1])
    s = start(behavior={"import_exclusion": False, "remove_from_seerr": False, "unmonitor": False,
                        "remove_episode_files": False})
    lab.delete_movie_files(m)
    lab.delete_episode_files(show, 1, [1])
    s.webhook(lab.movie_event(m))
    s.webhook(lab.episode_event(show, 1, 1))
    wait_for(lambda: len(s.pending()) == 2, what="both queued")
    assert lab.radarr.requests("DELETE")[0].query == {"deleteFiles": "true", "addImportExclusion": "false"}
    assert not lab.seerr.requests("DELETE")
    assert not lab.sonarr.requests("PUT")
    assert not lab.sonarr.requests("DELETE")


def test_second_radarr_keeps_its_copy(lab: Lab, start: StartFn, golden) -> None:
    uhd = lab.add_radarr_instance()
    m = lab.add_movie("Sintel", 2010, ratio=3, seeding_days=30)
    m4k = lab.add_movie("Sintel", 2010, radarr=uhd, release="Sintel.2010.2160p.WEB-DL", ratio=0.1)
    s = start()
    lab.delete_movie_files(m)
    s.webhook(lab.movie_event(m))
    s.wait_log("that copy stays")
    s.wait_log("^deleted torrent")
    assert len(lab.radarr.requests("DELETE")) == 1
    assert not uhd.requests("DELETE")
    assert not lab.seerr.requests("DELETE"), "the 4K copy is still there, so Seerr keeps it"
    assert m4k.torrent and m4k.torrent.hash in lab.torrents
    golden("log", s.log())
