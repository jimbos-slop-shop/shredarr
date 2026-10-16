"""Services that the lab tests can't reach end to end: notifications to Discord and Pushover, the daily summary,
update checks, and the torrent client's sign-in retry."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from shredarr.app import Shredarr
from shredarr.clients.base import ServiceError, ServiceHTTPError, ServiceUnreachable, SignInFailed, parse_body
from shredarr.config import NOTIFY_DEFAULTS
from shredarr.db import MIGRATIONS
from shredarr.services import notifications
from shredarr.services.cleanup import SweepContext
from shredarr.services.connections import TorrentService
from shredarr.services.health import Updates
from shredarr.services.settings_editor import SettingsEditor
from shredarr.settings import Settings
from shredarr.web.deps import client_ip
from tests.fakes.server import FakeServer, Response


@pytest.fixture
def hooks() -> Iterator[FakeServer]:
    fake = FakeServer()
    fake.route("POST", "/discord", lambda r: None)
    fake.route("PATCH", "/discord", lambda r: {"id": "1"})
    fake.route("POST", "/pushover", lambda r: {"status": 1})
    fake.route("POST", "/broken", lambda r: Response(500, "nope"))
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def core(tmp_path: Any) -> Shredarr:
    return Shredarr(Settings(config_path=str(tmp_path / "config.json"), db_path=str(tmp_path / "state.db")))


def test_discord_pushover_and_errors(hooks: FakeServer, monkeypatch: pytest.MonkeyPatch, core: Shredarr) -> None:
    monkeypatch.setattr(notifications, "PUSHOVER_URL", hooks.url + "/pushover")
    n = {
        **NOTIFY_DEFAULTS,
        "discord_url": hooks.url + "/discord",
        "discord_avatar": "https://x/a.png",
        "pushover_user": "u",
        "pushover_token": "t",
        "pushover_priority": "2",
        "pushover_sound": "magic",
        "ntfy_topic": "t",
        "ntfy_server": hooks.url + "/broken",
    }
    tried, errs = core.notifier.send(n, "Title", "x" * 5000)
    assert tried == ["Discord", "ntfy", "Pushover"]
    assert errs == ["ntfy: HTTP 500 nope"]
    discord = hooks.requests("POST", "/discord")[0]
    assert discord.json() == {
        "embeds": [{"title": "Title", "description": "x" * 4000, "color": 0x3B82F6}],
        "username": "Shredarr",
        "avatar_url": "https://x/a.png",
    }
    assert discord.headers["user-agent"] == "Shredarr"
    assert hooks.requests("POST", "/pushover")[0].form() == {
        "token": "t",
        "user": "u",
        "title": "Title",
        "message": "x" * 1024,
        "priority": "2",
        "sound": "magic",
        "retry": "60",
        "expire": "3600",
    }


def test_switches(core: Shredarr, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(core.notifier, "send_in_background", lambda n, title, msg: sent.append(title))
    core.config.save({"notify": {"on_cleanup": False}})
    core.notifier.notify("a", "", "cleanup")
    core.notifier.notify("b", "", "failure")
    core.notifier.notify("c", "", "jellyfin")  # off by default
    core.notifier.notify("d", "", "no switch")
    assert sent == ["b", "d"]


def test_discord_logo(hooks: FakeServer, core: Shredarr) -> None:
    url = hooks.url + "/discord"
    core.config.save({"notify": {"discord_url": url}})
    core.discord_logo()
    assert hooks.requests("PATCH", "/discord")[0].json()["avatar"].startswith("data:image/png;base64,")
    assert core.config.load().get("discord_logo_set") == url
    core.discord_logo()  # once per webhook
    assert len(hooks.requests("PATCH")) == 1


def test_daily_summary(core: Shredarr) -> None:
    core.db.migrate()
    core.history.add("a", 3 * 2**30, title="A", category=None, reason="seeding rule met", h=None, restore=None)
    core.activity.repo.add("Jellyfin deleted Movie: A", "info")
    core.activity.repo.add("  FAILED: x", "error")
    core.pending.add("b" * 40, "B", "movie")
    assert core.daily_summary() == (
        "Last 24 hours: 1 torrent cleaned up (3.0 GB freed), 1 delete from Jellyfin, 1 problem.\n"
        "Waiting for cleanup: 1 torrent."
    )


def test_update_check(core: Shredarr, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kw: Any) -> httpx.Response:
        assert url == "https://api.github.com/repos/me/shredarr/releases/latest"
        return httpx.Response(
            200, json={"tag_name": "v2.10.0", "html_url": "https://github.com/x"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    u = Updates(core.config, "me/shredarr", "2.9.1")
    u.check()
    assert u.info == {"latest": "2.10.0", "url": "https://github.com/x", "newer": True}
    core.config.save({"update_check": False})
    u.check()
    assert u.info == {}


def test_torrent_client_signs_in_again(core: Shredarr, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that stops accepting its session is made again once; an app that doesn't answer isn't retried."""
    outcomes: list[Exception | None] = [
        ServiceHTTPError(403),
        None,
        ServiceUnreachable("down"),
        ServiceError("odd"),
        None,
        SignInFailed("wrong password"),
    ]
    made: list[object] = []
    closed: list[object] = []

    class Client:
        def torrents(self, hashes: Any = None) -> list[Any]:
            fail = outcomes.pop(0)
            if fail:
                raise fail
            return []

        def close(self) -> None:
            closed.append(self)

    def make(kind: str, conf: Any, local: Any = None) -> Client:
        made.append(kind)
        return Client()

    monkeypatch.setattr("shredarr.services.connections.make_client", make)
    ts = TorrentService(core.connections)
    assert ts.all() == []  # 403, then a new client
    with pytest.raises(ServiceUnreachable):
        ts.all()
    assert ts.all() == []  # a client error, then a new client
    with pytest.raises(SignInFailed):  # a wrong password isn't tried twice
        ts.all()
    assert ts.all([]) == []  # no hashes asked for: no torrents, without asking the client
    assert len(made) == 4 and not outcomes
    assert len(closed) == 4  # every client that was thrown away was closed


def test_web_page_instead_of_json() -> None:
    """A reverse proxy's sign-in page is an error, not an answer; JSON is read even with leading whitespace."""
    page = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>sign in</html>")
    with pytest.raises(ServiceError, match="web page"):
        parse_body(page)
    assert parse_body(httpx.Response(200, headers={"content-type": "application/json"}, content=b' {"a": 1}')) == {
        "a": 1
    }
    assert parse_body(httpx.Response(200, content=b"ok")) == "ok"
    assert parse_body(httpx.Response(200, headers={"content-type": "application/json"}, content=b"")) == ""


def test_extra_instance_keys(core: Shredarr) -> None:
    editor = SettingsEditor(core.config, core.overrides, core.activity, lambda: None)
    core.db.migrate()
    editor.connections(
        {
            "extra_arrs": [
                {"kind": "radarr", "name": "Radarr 4K", "url": "http://a"},
                {"kind": "radarr", "name": "4K", "url": "http://b", "api_key": "k"},
                {"kind": "sonarr", "name": "", "url": "http://c"},
                {"kind": "sonarr", "name": "Sonarr", "url": "http://d"},
            ]
        }
    )
    keys = [x["key"] for x in core.config.load().get("extra_arrs")]
    assert keys == ["radarr-4k", "radarr-4k-2", "sonarr-2", "sonarr-2-2"]
    editor.connections({"extra_arrs": [{"key": "radarr-4k-2", "kind": "radarr", "name": "Renamed", "url": "http://b"}]})
    saved = core.config.load().get("extra_arrs")
    assert [(x["key"], x["name"], x["api_key"]) for x in saved] == [("radarr-4k-2", "Renamed", "k")]


def test_config_is_reread_when_it_changes(core: Shredarr) -> None:
    core.config.save({"dry_run": True})
    first = core.config.load()
    assert core.config.load() is first  # cached while unchanged
    with open(core.settings.config_path, "w") as f:
        json.dump({"dry_run": False}, f)
    time.sleep(0.01)
    assert core.config.load().dry_run is False


def test_config_file_is_private_from_the_start(core: Shredarr) -> None:
    with open(core.config.path + ".tmp", "w") as f:  # left over from an earlier save, with the default mode
        f.write("{}")
    os.chmod(core.config.path + ".tmp", 0o644)
    core.config.save({"dry_run": True})
    assert os.stat(core.config.path).st_mode & 0o777 == 0o600


def test_migration_finishes_a_step_that_was_cut_off(core: Shredarr) -> None:
    """A 1.x database where the log.level column was added but the version number wasn't saved."""
    core.db.migrate()
    con = sqlite3.connect(core.settings.db_path)
    con.execute("pragma user_version = 1")
    con.close()
    core.db.migrate()
    con = sqlite3.connect(core.settings.db_path)
    assert con.execute("pragma user_version").fetchone() == (len(MIGRATIONS),)
    con.close()


def test_sign_in_limit_address() -> None:
    """X-Forwarded-For only counts from a proxy on a private network, and only its last (proxy-added) entry."""

    def ip(peer: str, forwarded: str | None = None) -> str:
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return client_ip(Request({"type": "http", "client": (peer, 1234), "headers": headers}))

    assert ip("8.8.8.8") == "8.8.8.8"
    assert ip("8.8.8.8", "10.9.9.9") == "8.8.8.8"  # straight from the internet: the header is ignored
    assert ip("172.18.0.2", "198.51.100.7") == "198.51.100.7"  # a proxy in the Docker network
    assert ip("127.0.0.1", "1.1.1.1, 198.51.100.7") == "198.51.100.7"  # the first entry is whatever the client sent


def test_low_space_waits_when_sonarr_radarr_cant_be_asked(
    core: Shredarr, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without Sonarr/Radarr there's no telling whether a title rule protects a torrent: it isn't deleted early, and
    the sweep carries on."""
    core.db.migrate()
    core.config.save({"low_space": {"enabled": True, "min_free_gb": 10**9}})
    h = "a" * 40
    core.pending.add(h, "Spring", "leftover")
    t: Any = {"hash": h, "name": "Spring", "save_path": str(tmp_path), "content_path": str(tmp_path), "progress": 1}

    def down(*a: Any) -> None:
        raise ServiceUnreachable("Sonarr is down")

    monkeypatch.setattr(core.library, "in_library", lambda t: 0)
    monkeypatch.setattr(core.library, "title_of", down)
    deleted: list[Any] = []
    monkeypatch.setattr(core.cleaner, "cleanup", lambda *a, **k: deleted.append(a))
    core.cleaner.low_space_pass(core.config.load(), SweepContext({h: t}, {}), {})
    assert not deleted


def test_faked_forwarded_addresses_still_hit_a_limit(core: Shredarr) -> None:
    """Each wrong password counts for the address it claims and for the connection it came on (looser)."""
    lan = "192.168.1.50"
    for _ in range(9):
        core.auth.login_failed("10.0.0.1", lan)
    assert not core.auth.login_blocked("10.0.0.1", lan)
    core.auth.login_failed("10.0.0.1", lan)
    assert core.auth.login_blocked("10.0.0.1", lan)  # 10 for one address
    for i in range(40):
        core.auth.login_failed(f"10.1.0.{i}", lan)  # a new made-up address every time
    assert core.auth.login_blocked("10.2.0.1", lan)  # 50 on one connection
    assert not core.auth.login_blocked("10.2.0.1", "192.168.1.51")


def test_config_saves_where_chmod_is_refused(core: Shredarr, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any) -> None:
        raise PermissionError("not on this share")

    monkeypatch.setattr(os, "fchmod", refuse)
    core.config.save({"dry_run": True})
    assert core.config.load().dry_run


def test_cut_off_saved_torrents_are_cleaned_up(core: Shredarr) -> None:
    os.makedirs(core.saved.folder, exist_ok=True)
    old, new = (os.path.join(core.saved.folder, f"{c * 40}.torrent.tmp") for c in "ab")
    for p in (old, new):
        with open(p, "wb") as f:
            f.write(b"d8:announce")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    core.saved.prune()
    assert not os.path.exists(old) and os.path.exists(new)  # the new one may still be being written
    core.saved.delete_all()
    assert not os.path.exists(new)
