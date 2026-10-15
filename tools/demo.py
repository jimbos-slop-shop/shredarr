"""Run Shredarr with made-up data (for screenshots and trying the UI). Nothing is contacted or deleted.

    docker run --rm -p 8089:8089 -v "$PWD/tools:/tools" ghcr.io/jschaef80/shredarr python3 /tools/demo.py

Sign in with the password "demo".
"""
import json, os, sys, tempfile, threading, time

tmp = tempfile.mkdtemp(prefix="shredarr-demo-")
os.environ["SHREDARR_CONFIG"] = os.path.join(tmp, "config.json")
os.environ["SHREDARR_DB"] = os.path.join(tmp, "state.db")
sys.path.insert(0, "/app")
import shredarr as s  # noqa: E402

DAY, GB, NOW = 86400, 2**30, time.time()
conns = {k: {"url": f"http://{k}:{port}", "api_key": "demo"} for k, port in
         (("sonarr", 8989), ("radarr", 7878), ("jellyfin", 8096), ("seerr", 5055))}
conns["sonarr"]["url"] += "/api/v3"
conns["radarr"]["url"] += "/api/v3"
conns["seerr"]["url"] += "/api/v1"
conns["qbittorrent"] = {"url": "http://qbittorrent:8080", "username": "admin", "password": "demo"}
s.save_cfg({**conns, "rules": {"mode": "either", "min_seed_days": 14, "min_ratio": 2}, "grace": {"value": 1, "unit": "days"},
            "rule_list": [{"type": "category", "match": "tv-sonarr", "mode": "either", "min_seed_days": 7, "min_ratio": 1.5},
                          {"type": "title", "match": "The Lighthouse Keepers", "mode": "days", "min_seed_days": 3, "min_ratio": None},
                          {"type": "tracker", "match": "tracker.example.org", "mode": "days", "min_seed_days": 30, "min_ratio": None}],
            "notify": {"discord_url": "https://discord.com/api/webhooks/0000/demo", "discord_username": "Shredarr"}})
s.set_password("demo")

# name, title, category, size GB, ratio, seeded days, upload KB/s, in library, waiting for cleanup, own rule
T = [
    ("Big.Buck.Bunny.2008.1080p.BluRay.x264", "Big Buck Bunny", "movies-radarr", 1.9, 3.42, 41.2, 0, 1, False, None),
    ("Sintel.2010.2160p.WEB-DL.DDP5.1.HEVC", "Sintel", "movies-radarr", 6.4, 0.71, 9.3, 312, 1, False, None),
    ("Tears.of.Steel.2012.1080p.BluRay.x265", "Tears of Steel", "movies-radarr", 2.6, 2.18, 22.0, 0, 0, True, None),
    ("The.Lighthouse.Keepers.S02E05.1080p.WEB.h264", "The Lighthouse Keepers", "tv-sonarr", 1.4, 0.38, 4.8, 54, 0, True, None),
    ("The.Lighthouse.Keepers.S02E06.1080p.WEB.h264", "The Lighthouse Keepers", "tv-sonarr", 1.3, 0.22, 1.2, 128, 1, False, None),
    ("Harbor.Lights.S01.COMPLETE.1080p.WEB-DL.DDP5.1", "Harbor Lights", "tv-sonarr", 18.7, 1.06, 12.4, 0, 8, False, None),
    ("Cosmos.Laundromat.2015.1080p.WEB-DL", "Cosmos Laundromat", "movies-radarr", 1.1, 4.95, 63.1, 0, 0, True, None),
    ("Elephants.Dream.2006.1080p.BluRay.x264", "Elephants Dream", "movies-radarr", 1.2, 0.95, 3.6, 0, 0, False, None),
    ("Spring.2019.2160p.WEB-DL.HDR.HEVC", "Spring", "movies-radarr", 4.3, 1.52, 16.9, 0, 1, False, {"mode": "never"}),
    ("The.Night.Garden.S03E01.720p.HDTV.x264", "The Night Garden", "tv-sonarr", 0.8, 2.61, 30.2, 0, 1, False, None),
    ("Agent.327.Operation.Barbershop.2017.1080p", "Agent 327", "movies-radarr", 0.9, 0.12, 0.4, 0, 0, False, None),
    ("Big.Buck.Bunny.2008.1080p.BluRay.x264", "Big Buck Bunny", "movies-radarr", 1.9, 1.12, 20.1, 18, 1, False, None),  # cross-seed of row 0
]
TRACKERS = ["tracker.example.org", "opentracker.example.net"]
TRACKER_NAMES = {"tracker.example.org": "ExampleTracker", "opentracker.example.net": "OpenExample"}


def demo_torrents():
    c = s.cfg()
    out = []
    for i, (name, title, cat, size, ratio, days, up, lib, queued, own) in enumerate(T):
        t = {"hash": f"{i:040x}", "name": name, "category": cat, "tracker": "https://" + TRACKERS[i % 2] + "/announce",
             "seeding_time": days * DAY, "ratio": ratio, "progress": 1.0 if i != 7 else 0.64}
        r = {**own, "min_seed_days": None, "min_ratio": None, "source": "this torrent"} if own else s.rule_for(t, {}, c, title)
        progress, met = s.evaluate(r, t)
        out.append({"hash": t["hash"], "name": name, "title": title, "category": cat, "tracker": TRACKERS[i % 2],
                    "tracker_name": TRACKER_NAMES[TRACKERS[i % 2]], "cross_seed_of": T[0][0] if i == 11 else None,
                    "cross_seeds": 1 if i == 0 else 0, "with_original": False,
                    "state": "uploading" if up else ("downloading" if i == 7 else "stalledUP"), "progress": t["progress"],
                    "ratio": ratio, "seeding_days": days, "upspeed": up * 1024, "dlspeed": 4.2 * 2**20 if i == 7 else 0,
                    "size": int(size * GB), "added_on": int(NOW - (days + 1) * DAY), "rule": {k: r.get(k) for k in ("mode", "min_seed_days", "min_ratio")},
                    "rule_source": r["source"], "custom": bool(own), "rule_progress": progress, "rule_met": met, "tracked": True,
                    "days_met": days >= r["min_seed_days"] if r["mode"] not in ("ratio", "never") and r.get("min_seed_days") is not None else None,
                    "ratio_met": ratio >= r["min_ratio"] if r["mode"] not in ("days", "never") and r.get("min_ratio") is not None else None,
                    "in_library": lib, "leftover": i == 10, "queued": queued, "queued_as": title if queued else None,
                    "status": None, "delete_after": NOW + 5 * 3600 if i == 2 else None, "undo": "ok" if queued else None})
    return {"default": s.default_rule(c), "dry_run": False, "modes": s.MODES, "torrents": out,
            "freed": {"total": int(412.6 * GB), "count": 57, "last30": int(86.3 * GB)}, "health": s.health, "version": s.VERSION,
            "update": s.update_info, "free_gb": 1830}


ORPHANS = [("radarr:1", "radarr", "Caminandes", 2013, "Movie", 3.1, 1, "/data/media/movies/Caminandes (2013)"),
           ("sonarr:4:1", "sonarr", "Paper Moons", 2019, "S01E01-E10 (10 episodes)", 14.2, 10, "/data/media/tv/Paper Moons/Season 01"),
           ("sonarr:4:2", "sonarr", "Paper Moons", 2019, "S02E01-E08 (8 episodes)", 11.6, 8, "/data/media/tv/Paper Moons/Season 02"),
           ("radarr:2", "radarr", "Glass Half", 2015, "Movie", 1.4, 1, "/data/media/movies/Glass Half (2015)")]
s.media_without_torrent = lambda max_age=300: {"t": NOW, "items": sorted(
    [{"id": i, "app": a, "title": t, "year": y, "what": w, "size": int(g * GB), "files": f, "linked": False, "path": p}
     for i, a, t, y, w, g, f, p in ORPHANS], key=lambda x: -x["size"])}
s.orphan_plan = lambda c, ids: ([(f"Delete {', '.join(repr(x[2]) for x in ORPHANS if x[0] in ids)} from Jellyfin (and its library files)", None)]
                                + [(f"Remove '{x[2]}' from Radarr, with its files (and add an import exclusion)", None) for x in ORPHANS if x[0] in ids and x[1] == "radarr"]
                                + [(f"Unmonitor '{x[2]}' {x[4]} in Sonarr", None) for x in ORPHANS if x[0] in ids and x[1] == "sonarr"],
                                [(x[2], int(x[5] * GB), {}) for x in ORPHANS if x[0] in ids])
s.delete_orphans = lambda ids: None
s.torrents_json = demo_torrents
s.torrents = lambda **q: []
s.titles = lambda: {"t": NOW, "series": {1: "The Lighthouse Keepers", 2: "Harbor Lights", 3: "The Night Garden"},
                    "movies": {i: t[1] for i, t in enumerate(T) if t[2] == "movies-radarr"}}
s.check_health = s.check_update = lambda: None
s.free_gb = lambda ts: 1830
for k, v in {"sonarr": "Sonarr 4.0.20", "radarr": "Radarr 6.4.4", "qbittorrent": "qBittorrent v5.2.3", "jellyfin": "Jellyfin 10.11.10 · Home",
             "seerr": "Seerr 3.4.1"}.items():
    s.health[k] = {"ok": True, "msg": v, "ts": NOW}
s.update_info.update(latest=s.VERSION, url="https://github.com/jschaef80/shredarr/releases", newer=False)

with s.db() as con:
    for ago, msg in [(5400, "Jellyfin deleted Movie: Tears of Steel"), (5400, "  1 torrent(s) found in history, 1 newly queued for cleanup after seeding"),
                     (5400, "  removed 'Tears of Steel' from Radarr (with import exclusion)"), (5400, "  removed 'Tears of Steel' from Seerr"),
                     (3000, "Jellyfin deleted Episode: The Lighthouse Keepers / Undertow (S2E5)"),
                     (3000, "  unmonitored 'The Lighthouse Keepers' S02E05 in Sonarr"),
                     (3000, "  removed 1 episode file record(s) of 'The Lighthouse Keepers' S02E05 from Sonarr"),
                     (1800, "deleted torrent 'Wing.It.2023.1080p.WEB-DL' and its data (seeded 14.2 of 14 days, ratio 1.10 of 2)"),
                     (900, "Undo: Cosmos Laundromat"), (900, "  done: re-added to Radarr"), (900, "  done: took 1 torrent off the cleanup list"),
                     (900, "  done: re-imported 'Cosmos.Laundromat.2015.1080p.WEB-DL': 1 file back in the library"),
                     (300, "Default rule saved: 14 days or ratio 2, seed time OR ratio (whichever first) · live")]:
        con.execute("insert into log (ts, msg) values (?, ?)", (NOW - ago, msg))
    for ago, name, title, size, reason in [(1800, "Wing.It.2023.1080p.WEB-DL", "Wing It!", 1.7, "seeding rule met"),
                                           (2 * DAY, "Charge.2022.2160p.WEB-DL.HDR", "Charge", 5.9, "seeding rule met"),
                                           (4 * DAY, "Harbor.Lights.S00.Specials.1080p.WEB", "Harbor Lights", 3.2, "Delete now"),
                                           (9 * DAY, "Sprite.Fright.2021.1080p.BluRay", "Sprite Fright", 2.4, "low disk space"),
                                           (16 * DAY, "The.Night.Garden.S02.1080p.WEB-DL", "The Night Garden", 22.8, "seeding rule met")]:
        con.execute("insert into cleaned (ts, name, size, title, category, reason) values (?,?,?,?,?,?)",
                    (NOW - ago, name, int(size * GB), title, "tv-sonarr" if "S0" in name else "movies-radarr", reason))

print("Shredarr demo on http://localhost:8089 (password: demo)", flush=True)
s.ThreadingHTTPServer(("0.0.0.0", 8089), s.Handler).serve_forever()
