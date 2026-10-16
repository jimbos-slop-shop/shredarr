"""Run Shredarr with made-up data, for trying the UI and taking screenshots. Nothing real is contacted or deleted:
Shredarr itself runs unchanged, against the same fake Sonarr, Radarr, qBittorrent, Jellyfin and Seerr the tests use.

    uv run tools/demo.py

Then open http://localhost:8089 and sign in with the password "demo".
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shredarr.app import Shredarr
from shredarr.settings import Settings
from shredarr.web.app import create_app
from tests.fakes.lab import OTHER_TRACKER, Lab

GB, DAY, NOW = 2**30, 86400, time.time()


def build(lab: Lab) -> list[dict[str, object]]:
    """The library, the downloads and what was just deleted in Jellyfin. Returns the webhook events to send."""
    gb = lambda x: int(x * GB)  # noqa: E731
    bunny = lab.add_movie("Big Buck Bunny", 2008, ratio=3.42, seeding_days=41.2, size=gb(1.9))
    lab.add_movie(
        "Sintel", 2010, release="Sintel.2010.2160p.WEB-DL.DDP5.1.HEVC", ratio=0.71, seeding_days=9.3, size=gb(6.4)
    )
    steel = lab.add_movie("Tears of Steel", 2012, ratio=2.18, seeding_days=22, size=gb(2.6))
    cosmos = lab.add_movie("Cosmos Laundromat", 2015, ratio=4.95, seeding_days=63.1, size=gb(1.1))
    dream = lab.add_movie("Elephants Dream", 2006, ratio=0.95, seeding_days=3.6, size=gb(1.2))
    lab.add_movie(
        "Spring", 2019, release="Spring.2019.2160p.WEB-DL.HDR.HEVC", ratio=1.52, seeding_days=16.9, size=gb(4.3)
    )
    agent = lab.add_movie("Agent 327", 2017, ratio=0.12, seeding_days=0.4, size=gb(0.9))
    lab.add_movie("Caminandes", 2013, torrent=False, size=gb(3.1))
    assert bunny.torrent and dream.torrent and steel.torrent
    lab.add_cross_seed(bunny.torrent, ratio=1.12, seeding_days=20.1).upspeed = 18 * 1024
    dream.torrent.progress, dream.torrent.dlspeed = 0.64, int(4.2 * 2**20)
    lab.torrents[next(h for h, t in lab.torrents.items() if t.name.startswith("Sintel"))].upspeed = 312 * 1024

    keepers = lab.add_series("The Lighthouse Keepers", seasons={2: 6})
    lab.add_release(keepers, 2, [5], ratio=0.38, seeding_days=4.8, size=gb(1.4)).upspeed = 54 * 1024
    lab.add_release(keepers, 2, [6], ratio=0.22, seeding_days=1.2, size=gb(1.3)).upspeed = 128 * 1024
    harbor = lab.add_series("Harbor Lights", seasons={1: 8})
    lab.add_release(harbor, 1, range(1, 9), ratio=1.06, seeding_days=12.4, size=gb(18.7 / 8))
    garden = lab.add_series("The Night Garden", seasons={3: 4})
    lab.add_release(
        garden, 3, [1], release="The.Night.Garden.S03E01.720p.HDTV.x264", ratio=2.61, seeding_days=30.2, size=gb(0.8)
    )
    lab.add_release(
        garden, 3, [2], tracker=OTHER_TRACKER, indexer="OpenExample", ratio=0.4, seeding_days=6, size=gb(0.8)
    )
    moons = lab.add_series("Paper Moons", year=2019, seasons={1: 10, 2: 8})
    lab.add_library_episodes(moons, 1, range(1, 11), size=gb(1.42))
    lab.add_library_episodes(moons, 2, range(1, 9), size=gb(1.45))

    for m in (steel, cosmos):
        lab.delete_movie_files(m)
    lab.delete_movie_files(agent)  # gone from the library, but never reported: a leftover
    lab.delete_episode_files(keepers, 2, [5])
    return [lab.movie_event(steel), lab.movie_event(cosmos), lab.episode_event(keepers, 2, 5)]


def history(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    rows = [
        (1800, "Wing.It.2023.1080p.WEB-DL", "Wing It!", 1.7, "seeding rule met"),
        (2 * DAY, "Charge.2022.2160p.WEB-DL.HDR", "Charge", 5.9, "seeding rule met"),
        (4 * DAY, "Harbor.Lights.S00.Specials.1080p.WEB", "Harbor Lights", 3.2, "Delete now"),
        (9 * DAY, "Sprite.Fright.2021.1080p.BluRay", "Sprite Fright", 2.4, "low disk space"),
        (16 * DAY, "The.Night.Garden.S02.1080p.WEB-DL", "The Night Garden", 22.8, "seeding rule met"),
    ]
    for ago, name, title, size, reason in rows:
        con.execute(
            "insert into cleaned (ts, name, size, title, category, reason) values (?,?,?,?,?,?)",
            (NOW - ago, name, int(size * GB), title, "tv-sonarr" if ".S0" in name else "radarr", reason),
        )
    con.commit()
    con.close()


def main() -> None:
    root = tempfile.mkdtemp(prefix="shredarr-demo-", dir="/tmp")
    lab = Lab(os.path.join(root, "lab"))
    lab.add_radarr_instance()
    lab.add_movie("Sintel", 2010, radarr=lab.extra_arrs[0][1], torrent=False, size=int(6.8 * GB))  # type: ignore[arg-type]
    events = build(lab)
    config = lab.config(
        prowlarr=True,
        grace={"value": 1, "unit": "days"},
        rule_list=[
            {"type": "category", "match": "tv-sonarr", "mode": "either", "min_seed_days": 7, "min_ratio": 1.5},
            {"type": "title", "match": "The Lighthouse Keepers", "mode": "days", "min_seed_days": 3, "min_ratio": None},
            {"type": "tracker", "match": "othersite.net", "mode": "days", "min_seed_days": 30, "min_ratio": None},
            {"type": "title", "match": "Spring", "mode": "never", "min_seed_days": None, "min_ratio": None},
        ],
    )
    settings = Settings(
        config_path=os.path.join(root, "config", "config.json"), db_path=os.path.join(root, "config", "state.db")
    )
    core = Shredarr(settings)
    core.config.save(config)
    core.auth.set_password("demo")
    core.db.migrate()
    history(settings.db_path)
    for ev in events:
        core.events.put(ev)
    port = int(os.environ.get("SHREDARR_PORT", "8089"))
    print(f"Shredarr demo on http://localhost:{port} (password: demo). Files in {root}", flush=True)
    uvicorn.run(create_app(core), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
