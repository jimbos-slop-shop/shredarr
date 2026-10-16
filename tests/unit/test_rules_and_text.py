from __future__ import annotations

import pytest

from shredarr.clients.torrents import Torrent
from shredarr.config import Config, api_url, base_url
from shredarr.files import PathMapper, first_rar_volumes
from shredarr.services.rules import evaluate, rule_for
from shredarr.services.trackers import site_of
from shredarr.text import duration, episodes_label, human, rule_text, tail

DAY = 86400


def torrent(**kw: object) -> Torrent:
    t: dict[str, object] = {
        "hash": "a" * 40,
        "name": "x",
        "save_path": "/d",
        "content_path": "/d/x",
        "size": 1,
        "progress": 1.0,
        "ratio": 1.0,
        "seeding_time": 10 * DAY,
        "state": "stalledUP",
        "upspeed": 0,
        "dlspeed": 0,
        "added_on": 0,
        "category": "tv",
        "tracker": "https://tracker.example.org/passkey/announce",
    }
    t.update(kw)
    return t  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("mode", "days", "ratio", "met", "text"),
    [
        ("either", 14, 2, False, "seeded 10.0 of 14 days, ratio 1.00 of 2"),
        ("either", 7, 2, True, "seeded 10.0 of 7 days, ratio 1.00 of 2"),
        ("both", 7, 2, False, "seeded 10.0 of 7 days, ratio 1.00 of 2"),
        ("both", 7, 1, True, "seeded 10.0 of 7 days, ratio 1.00 of 1"),
        ("days", 7, 99, True, "seeded 10.0 of 7 days"),
        ("ratio", 1, 2, False, "ratio 1.00 of 2"),
        ("either", None, None, True, "no seeding rule"),
        ("never", 0, 0, False, "protected: never deleted"),
    ],
)
def test_evaluate(mode: str, days: float | None, ratio: float | None, met: bool, text: str) -> None:
    assert evaluate({"mode": mode, "min_seed_days": days, "min_ratio": ratio}, torrent()) == (text, met)


def test_still_downloading_never_meets_a_rule() -> None:
    assert evaluate({"mode": "either", "min_seed_days": 0, "min_ratio": 0}, torrent(progress=0.5))[1] is False


def test_rule_precedence() -> None:
    rules = [
        {"type": "category", "match": "tv", "mode": "days", "min_seed_days": 1},
        {"type": "tracker", "match": "example.org", "mode": "days", "min_seed_days": 2},
        {"type": "tracker", "match": "TorrentDay", "mode": "days", "min_seed_days": 5},
        {"type": "title", "match": "the show", "mode": "days", "min_seed_days": 3},
    ]
    c = Config({"rule_list": rules, "rules": {"mode": "ratio", "min_ratio": 4}})

    def rule(t: Torrent, title: str | None = None, overrides: dict | None = None, names: dict | None = None) -> object:
        return rule_for(t, overrides or {}, c, title, lambda: names or {})["source"]

    t = torrent()
    assert rule(t, "The Show") == "the show"
    assert rule(t) == "example.org"  # announce host ends with .example.org
    assert rule(torrent(tracker="https://other.net/a")) == "tv"
    assert rule(torrent(tracker="https://other.net/a"), names={t["hash"]: "torrentday"}) == "TorrentDay"
    assert rule(torrent(tracker="", category="movies")) == "default"
    assert rule_for(torrent(tracker="", category="movies"), {}, c, None, dict)["mode"] == "ratio"
    assert rule(t, "The Show", overrides={t["hash"]: {"mode": "never"}}) == "this torrent"


def test_texts() -> None:
    assert [human(n) for n in (512, 2048, 5 * 2**20, 3.25 * 2**30, 2**42)] == [
        "512 B",
        "2 KB",
        "5 MB",
        "3.2 GB",
        "4.0 TB",
    ]
    assert [duration(s) for s in (5, 3700, 2 * DAY + 3 * 3600, -1)] == ["1m", "1h 1m", "2d 3h", "1m"]
    eps = [{"seasonNumber": 1, "episodeNumber": n} for n in (3, 1, 2)] + [{"seasonNumber": 2, "episodeNumber": 5}]
    assert episodes_label(eps) == "S01E01-E03 (3 episodes), S02E05"
    assert rule_text({"mode": "both", "min_seed_days": 7.0, "min_ratio": 1.5}) == "7 days and ratio 1.5"
    assert rule_text({"mode": "ratio", "min_seed_days": 7, "min_ratio": None}) == "no rule"
    assert rule_text({"mode": "never"}) == "never delete"
    assert tail("/media/tv/Show/Season 01/x.mkv") == "Season 01/x.mkv" == tail("D:\\tv\\Season 01\\x.mkv")


def test_site_of() -> None:
    assert site_of("tracker.torrentleech.org") == "torrentleech.org"
    assert site_of("announce.site.co.uk") == "site.co.uk"
    assert site_of(None) == ""


def test_urls() -> None:
    assert base_url("sonarr", " http://sonarr:8989/api/v3/ ") == "http://sonarr:8989"
    assert api_url("radarr-4k", "http://radarr:7878") == "http://radarr:7878/api/v3"
    assert api_url("seerr", "http://seerr:5055/api/v1") == "http://seerr:5055/api/v1"
    assert api_url("jellyfin", "http://jf:8096/") == "http://jf:8096"
    assert api_url("sonarr", "") == ""


def test_config_defaults_and_old_settings() -> None:
    c = Config(
        {
            "notify": {"ntfy_url": "https://ntfy.example.com/my-topic"},
            "restore": {"method": "off"},
            "rules": {"require": "both", "min_seed_days": 3},
            "grace": {"value": 2, "unit": "days"},
            "torrent_client": "bogus",
            "sonarr": {"url": "http://s"},
            "extra_arrs": [{"key": "radarr-4k", "kind": "radarr", "url": "http://r4"}, {"key": "x", "kind": "radarr"}],
        }
    )
    assert (c.notify["ntfy_server"], c.notify["ntfy_topic"]) == ("https://ntfy.example.com", "my-topic")
    assert "ntfy_url" not in c.notify and c.notify["summary_time"] == "09:00"
    assert c.restore_enabled is False
    assert c.default_rule == {"mode": "both", "min_seed_days": 3, "min_ratio": None}
    assert c.grace_seconds == 2 * DAY
    assert c.client_kind == "qbittorrent" and c.media_kind == "jellyfin"
    assert c.missing == ["radarr", "qbittorrent", "jellyfin"]
    assert [(x.key, x.name) for x in c.arr_instances()] == [("sonarr", "Sonarr"), ("radarr-4k", "radarr-4k")]
    assert c.arr_name("radarr-gone") == "Radarr"
    assert Config({}).default_rule == {"mode": "either", "min_seed_days": 14, "min_ratio": 2}


@pytest.mark.xfail(strict=True, reason="1.x bug: .part10.rar and up count as first volumes (fixed in a later commit)")
def test_files() -> None:
    assert first_rar_volumes(["a.rar", "a.r00", "b.part1.rar", "b.part2.rar", "b.part10.rar", "c.part01.rar"]) == [
        "a.rar",
        "b.part1.rar",
        "c.part01.rar",
    ]
    m = PathMapper([{"remote": "/data/", "local": "/mnt/data/"}])
    assert m.local("/data/x.mkv") == "/mnt/data/x.mkv" and m.remote("/mnt/data/x.mkv") == "/data/x.mkv"
    assert m.local("/other/x") == "/other/x" and m.local(None) == ""
