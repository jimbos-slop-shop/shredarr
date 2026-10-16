"""Sign-in, pages, settings and connections."""

from __future__ import annotations

import os
import re
import time

import httpx
import pytest

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import Shredarr, lan_ip, wait_for

LAN = lan_ip()
needs_lan = pytest.mark.skipif(LAN is None, reason="needs a non-loopback address to test sign-in")


def remote(s: Shredarr) -> httpx.Client:
    """A client that isn't on the same machine, so sign-in applies."""
    return httpx.Client(base_url=f"http://{LAN}:{s.port}", timeout=30, follow_redirects=False)


@needs_lan
def test_first_run_setup_and_sign_in(lab: Lab, start: StartFn, golden) -> None:
    s = start()
    c = remote(s)
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert c.get("/api/torrents").status_code == 401
    assert c.post("/api/rule", json={}).status_code == 401
    assert c.get("/login").headers["location"] == "/setup"
    page = c.get("/setup")
    assert page.status_code == 200 and "Choose a password" in page.text
    assert c.post("/setup", data={"new": "abc", "confirm": "abd"}).status_code == 400
    r = c.post("/setup", data={"new": "abc", "confirm": "abc"})
    assert r.status_code == 400 and "at least 8 characters" in r.text
    r = c.post("/setup", data={"new": "correct horse", "confirm": "correct horse"})
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert re.search(r"shredarr_session=[\w-]+; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax$", r.headers["set-cookie"])
    assert c.get("/api/torrents").status_code == 200
    assert c.post("/setup", data={"new": "x", "confirm": "x"}).status_code == 403
    assert set(s.read_config()["auth"]) == {"salt", "hash"}

    other = remote(s)
    r = other.get("/")
    assert r.headers["location"] == "/login"
    r = other.post("/login", data={"password": "wrong"})
    assert r.status_code == 401 and "That password isn&#x27;t right." in r.text
    r = remote(s).post("/login", data={"password": "correct horse"}, headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 303 and r.headers["set-cookie"].endswith("; Secure")
    assert other.post("/login", data={"password": "correct horse"}).status_code == 303
    assert other.get("/login").headers["location"] == "/"
    assert other.get("/api/health").status_code == 200

    # change the password: other devices are signed out
    r = c.post("/api/password", json={"current": "nope", "new": "something long"})
    assert r.status_code == 400 and r.text == "Your current password isn't right."
    assert c.post("/api/password", json={"current": "correct horse", "new": "x"}).status_code == 400
    assert c.post("/api/password", json={"current": "correct horse", "new": "battery staple"}).status_code == 200
    assert c.get("/api/health").status_code == 200
    assert other.get("/api/health").status_code == 401
    r = c.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/health").status_code == 401
    golden("log", s.log())


@needs_lan
def test_too_many_wrong_passwords(lab: Lab, start: StartFn) -> None:
    s = start()
    assert s.cli("set-password", "sesame!!").returncode == 0
    c = remote(s)
    for _ in range(10):
        assert c.post("/login", data={"password": "no"}).status_code == 401
    r = c.post("/login", data={"password": "sesame!!"})
    assert r.status_code == 429 and "Too many attempts" in r.text


@needs_lan
def test_password_reset_with_a_code(lab: Lab, start: StartFn) -> None:
    s = start()
    s.cli("set-password", "first password")
    c = remote(s)
    assert c.get("/reset").status_code == 200
    r = c.post("/reset/code")
    assert r.status_code == 200 and "Code sent" in r.text
    assert c.post("/reset/code").status_code == 429
    with open(os.path.join(s.config_dir, "RESET-CODE.txt")) as f:
        code = re.search(r"code: (\w{4}-\w{4}-\w{4})", f.read()).group(1)  # type: ignore[union-attr]
    wait_for(lambda: "Shredarr password reset" in lab.ntfy.titles(), what="reset code notification")
    assert code in lab.ntfy.messages()[-1]["message"]
    assert c.post("/reset", data={"code": "AAAA-BBBB-CCCC", "new": "new password", "confirm": "new password"}).status_code == 400
    r = c.post("/reset", data={"code": code.lower(), "new": "new password", "confirm": "new password"})
    assert r.status_code == 303 and "shredarr_session=" in r.headers["set-cookie"]
    assert not os.path.exists(os.path.join(s.config_dir, "RESET-CODE.txt"))
    assert remote(s).post("/login", data={"password": "new password"}).status_code == 303
    assert "Password reset with a reset code (all devices were signed out)" in s.log()


def test_public_paths_and_page(lab: Lab, start: StartFn) -> None:
    s = start()
    assert s.get("/health").text == "ok"
    for p, ctype in (("/logo.svg", "image/svg+xml"), ("/logo.png", "image/png"), ("/favicon.ico", "image/png"),
                     ("/static/app.js", "text/javascript"), ("/static/app.css", "text/css")):
        r = s.get(p)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), p
        assert r.headers["cache-control"] == "max-age=86400"
    assert s.get("/static/index.html").status_code == 404
    assert s.get("/static/../shredarr.py").status_code == 404
    assert s.get("/nope").status_code == 404
    for path in ("/", "/settings"):
        page = s.get(path)
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert "window.MODES" in page.text and '"either"' in page.text
        assert not re.search(r"%[A-Z]+%", page.text), "every placeholder filled in"


def test_settings_api_hides_secrets(lab: Lab, start: StartFn, golden) -> None:
    lab.add_radarr_instance()
    lab.add_movie("Sintel", 2010, category="radarr")
    s = start(prowlarr=True, rule_list=[{"type": "category", "match": "radarr", "mode": "never"}])
    st = s.api("/api/settings")
    assert "radarr-key" not in str(st) and "adminadmin" not in str(st) and "a1b2c3d4e5" not in str(st)
    st["health"] = {k: {**v, "ts": 0} for k, v in st["health"].items()}
    golden("settings", st)


def test_save_connections(lab: Lab, start: StartFn, golden) -> None:
    s = start()
    r = s.post("/api/connections", {
        "sonarr": {"url": lab.sonarr.url + "/", "api_key": ""},  # blank key: keep the saved one
        "radarr": {"url": lab.radarr.url + "/api/v3", "api_key": "new-key"},
        "seerr": {"url": ""},  # optional and blank: removed
        "plex": {"url": "http://plex:32400", "api_key": "t"},
        "media_server": "jellyfin", "torrent_client": "qbittorrent",
        "extra_arrs": [{"kind": "radarr", "name": "Radarr 4K", "url": "http://radarr4k:7878", "api_key": "k4"},
                       {"kind": "radarr", "name": "Radarr 4K", "url": "http://other:7878", "api_key": "k5"},
                       {"kind": "bogus", "url": "http://x"}, {"kind": "sonarr", "url": ""}],
        "unknown": {"url": "http://x"},
    })
    assert r.status_code == 200
    c = s.read_config()
    golden("config", {k: c[k] for k in ("sonarr", "radarr", "plex", "extra_arrs") if k in c} | {"has_seerr": "seerr" in c})
    s.wait_log("Connection settings saved")
    assert s.post("/api/behavior", {"unmonitor": False, "bogus": True}).status_code == 200
    assert s.read_config()["behavior"] == {"import_exclusion": True, "remove_from_seerr": True, "unmonitor": False,
                                           "remove_episode_files": True, "cross_seed_with_original": False}
    assert s.post("/api/update-check", {"enabled": False}).status_code == 200
    assert s.read_config()["update_check"] is False
    rules = [{"type": "tracker", "match": " example.org ", "mode": "days", "min_seed_days": "3", "min_ratio": ""},
             {"type": "title", "match": "", "mode": "days"}, {"type": "x", "match": "y", "mode": "days"},
             {"type": "category", "match": "tv", "mode": "bogus"}]
    assert s.post("/api/rule-list", {"rules": rules}).status_code == 200
    assert s.read_config()["rule_list"] == [{"type": "tracker", "match": "example.org", "mode": "days", "min_seed_days": 3.0,
                                             "min_ratio": None}]
    assert s.post("/api/password-policy", {"min_length": "12", "upper": 1, "digit": 0, "symbol": True}).status_code == 200
    assert s.read_config()["auth_policy"] == {"min_length": 12, "upper": True, "digit": False, "symbol": True}
    assert s.post("/api/password-policy", {"min_length": "x"}).status_code == 400
    golden("log", s.log())


def test_connection_tests(lab: Lab, start: StartFn, golden) -> None:
    s = start()
    out = {}
    for label, svc, conf in [
        ("sonarr", "sonarr", {"url": lab.sonarr.url}),  # saved key used when none is typed
        ("radarr-bad-key", "radarr", {"url": lab.radarr.url + "/api/v3", "api_key": "bad"}),
        ("radarr-4k", "radarr-4k", {"url": lab.radarr.url, "api_key": lab.radarr.api_key}),
        ("prowlarr", "prowlarr", {"url": lab.prowlarr.url, "api_key": "x"}),
        ("seerr", "seerr", {"url": lab.seerr.url}),
        ("qbittorrent", "qbittorrent", {"url": lab.qbittorrent.url, "username": "admin"}),
        ("qbittorrent-bad", "qbittorrent", {"url": lab.qbittorrent.url, "username": "admin", "password": "nope"}),
        ("transmission", "transmission", {"url": lab.transmission.url}),
        ("deluge", "deluge", {"url": lab.deluge.url, "password": "deluge"}),
        ("deluge-bad", "deluge", {"url": lab.deluge.url, "password": "no"}),
        ("jellyfin", "jellyfin", {"url": lab.jellyfin.url}),
        ("jellyfin-bad", "jellyfin", {"url": lab.jellyfin.url, "api_key": "nope"}),
        ("plex", "plex", {"url": lab.plex.url, "api_key": lab.plex.token}),
        ("sonarr-down", "sonarr", {"url": "http://127.0.0.1:9", "api_key": "x"}),
        ("unknown", "nothing", {"url": "http://127.0.0.1:9"}),
        ("empty", "sonarr", {"url": ""}),
    ]:
        r = s.post("/api/test-connection", {"service": svc, "conf": conf})
        out[label] = r.json()
    out["sonarr-down"]["msg"] = re.sub(r"\[Errno \d+\] ", "", out["sonarr-down"]["msg"])  # 61 on macOS, 111 on Linux
    golden("results", out)


def test_health_and_connection_failures(lab: Lab, start: StartFn) -> None:
    s = start()
    h = wait_for(lambda: len(s.api("/api/health")) == 5 and s.api("/api/health"), what="health checks")
    assert {k: v["ok"] for k, v in h.items()} == {"sonarr": True, "radarr": True, "qbittorrent": True, "jellyfin": True,
                                                  "seerr": True}
    lab.seerr.stop()
    s.post("/api/connections", {})
    s.wait_log("Connection FAILED: can't reach Seerr")
    wait_for(lambda: "Shredarr hit a problem" in lab.ntfy.titles(), what="failure notification")


def test_jellyfin_webhook_setup(lab: Lab, start: StartFn, golden) -> None:
    s = start()
    r = s.post("/api/jellyfin-webhook", {"url": "http://shredarr:8089/"})
    assert r.status_code == 200 and r.text == "Jellyfin will now send deletes to http://shredarr:8089/webhook/jellyfin"
    golden("plugin", lab.jellyfin.plugin_config)
    st = s.api("/api/settings")
    assert st["webhook_url_guess"] == "http://shredarr:8089"
    lab.jellyfin.plugin_config = {"GenericOptions": []}
    lab.jellyfin.routes = [x for x in lab.jellyfin.routes if x[1].pattern != "/Plugins/?$"]
    lab.jellyfin.route("GET", "/Plugins", lab.jellyfin.auth(lambda r: []))
    r = s.post("/api/jellyfin-webhook", {"url": "http://shredarr:8089/"})
    assert r.status_code == 400 and "the Webhook plugin isn't installed" in r.text


def test_notifications(lab: Lab, start: StartFn) -> None:
    s = start(notify=False)
    assert s.post("/api/notify-test").status_code == 400
    bad = s.post("/api/settings", {"notify": {"discord_url": "https://example.com/hook"}})
    assert bad.status_code == 400 and bad.text == "That doesn't look like a Discord webhook URL."
    assert s.post("/api/settings", {"notify": {"summary_time": "9am"}}).status_code == 400
    ok = s.post("/api/settings", {"notify": {"ntfy_server": lab.ntfy.url, "ntfy_topic": " lab ", "ntfy_priority": 4,
                                             "on_cleanup": 0, "junk": "x"}})
    assert ok.status_code == 200
    assert s.read_config()["notify"] == {"ntfy_server": lab.ntfy.url, "ntfy_topic": "lab", "ntfy_priority": "4", "on_cleanup": False}
    r = s.post("/api/notify-test")
    assert r.status_code == 200 and r.text == "Sent to ntfy"
    assert lab.ntfy.messages()[-1] == {"topic": "lab", "title": "Shredarr test", "message": "Notifications are working. 🗑️",
                                       "tags": ["wastebasket"], "priority": 4}
    st = s.api("/api/settings")["notify"]
    assert st["ntfy_topic"] == "lab" and st["on_cleanup"] is False and st["discord_username"] == "Shredarr"


def test_unconfigured(lab: Lab, start: StartFn, golden) -> None:
    s = start(config=None)
    t = s.api("/api/torrents")
    assert t["setup"] == ["sonarr", "radarr", "qbittorrent", "jellyfin"] and t["torrents"] == []
    st = s.api("/api/settings")
    assert st["missing"] == ["sonarr", "radarr", "qbittorrent", "jellyfin"]
    assert len(s.read_config()["webhook_token"]) == 64
    golden("settings", {k: v for k, v in st.items() if k != "health"})
    time.sleep(0.5)
    assert s.log() == ["Shredarr started"]
