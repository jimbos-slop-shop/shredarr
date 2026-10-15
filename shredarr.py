#!/usr/bin/env python3
"""Shredarr: when something is deleted in Jellyfin, remove it from Sonarr/Radarr/Seerr
right away, then delete its torrent (and download copy) from qBittorrent once the
seeding rules are met and none of the torrent's files are still in the library."""
import base64, hashlib, hmac, html, http.cookiejar, http.cookies, json, os, queue, re, secrets, shutil, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
REPO = os.environ.get("SHREDARR_REPO", "jschaef80/shredarr")  # where update checks look for new releases
CFG = os.environ.get("SHREDARR_CONFIG", "/config/config.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png", ".html": "text/html"}


def read_static(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()
DB = os.environ.get("SHREDARR_DB", "/config/state.db")
VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")
ARCHIVE = re.compile(r"\.(rar|r\d\d|\d\d\d)$", re.I)
def grace_seconds():
    """Deletion delay: how long a torrent waits after its media is deleted in Jellyfin before it can be removed,
    even once its seeding rule is met (time to Undo). 0 = no delay."""
    g = cfg().get("grace", {})
    return float(g.get("value") or 0) * (86400 if g.get("unit") == "days" else 3600)


def duration(sec):
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def restore_enabled():
    """Undo of RAR releases: unpack the torrent's archives with the bundled unar. On unless switched off in Settings."""
    r = cfg().get("restore", {})
    return r.get("enabled", r.get("method") != "off")
events = queue.Queue()
suppressed = set()  # Jellyfin item ids deleted by "Delete now"; their webhooks are ignored
sweep_lock = threading.Lock()


def cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_cfg(c):
    os.makedirs(os.path.dirname(CFG) or ".", exist_ok=True)
    tmp = CFG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG)


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("create table if not exists pending (hash text primary key, name text, source text, added_at real, status text)")
    con.execute("create table if not exists log (id integer primary key, ts real, msg text)")
    cols = {r[1] for r in con.execute("pragma table_info(pending)")}
    for col, typ in (("rule_mode", "text"), ("rule_days", "real"), ("rule_ratio", "real"), ("undo", "text"), ("event", "text"), ("content", "text")):
        if col not in cols:
            con.execute(f"alter table pending add column {col} {typ}")
    # per-torrent seeding rules; any torrent without a row follows the default rule
    con.execute("create table if not exists overrides (hash text primary key, mode text, days real, ratio real)")
    con.execute("insert or ignore into overrides select hash, rule_mode, rule_days, rule_ratio from pending where rule_mode is not null")
    con.execute("update pending set rule_mode = null where rule_mode is not null")
    con.execute("create table if not exists sessions (token text primary key, created real, expires real)")
    con.execute("create table if not exists redownloads (hash text primary key, ts real, app text, title text, tries integer default 0, last_try real, done integer default 0)")
    con.execute("create table if not exists cleaned (ts real, name text, size integer)")
    have = {r[1] for r in con.execute("pragma table_info(cleaned)")}
    for col in ("title", "category", "reason", "hash", "restore", "redl_ts", "redl_how"):
        if col not in have:
            con.execute(f"alter table cleaned add column {col} text")
    return con


MODES = {"either": "seed time OR ratio (whichever first)", "both": "seed time AND ratio",
         "days": "seed time only", "ratio": "ratio only", "never": "never delete (protected)"}
SERVICES = {"sonarr": "Sonarr", "radarr": "Radarr", "seerr": "Seerr", "qbittorrent": "qBittorrent", "jellyfin": "Jellyfin"}
REQUIRED = ("sonarr", "radarr", "qbittorrent", "jellyfin")
SECRET_KEYS = ("api_key", "password")  # never sent back to the browser
API_SUFFIX = {"sonarr": "/api/v3", "radarr": "/api/v3", "seerr": "/api/v1"}
BEHAVIOR_DEFAULTS = {"import_exclusion": True, "remove_from_seerr": True, "unmonitor": True, "remove_episode_files": True}
LOW_SPACE_DEFAULTS = {"enabled": False, "min_free_gb": 100}


def behavior():
    return {**BEHAVIOR_DEFAULTS, **cfg().get("behavior", {})}


def configured(c=None):
    c = c if c is not None else cfg()
    return [k for k in REQUIRED if not (c.get(k) or {}).get("url")]


def base_url(svc, url):
    """What the user typed (http://sonarr:8989) -> what the API client needs (…/api/v3)."""
    url = (url or "").strip().rstrip("/")
    suffix = API_SUFFIX.get(svc, "")
    if suffix and url.endswith(suffix):
        url = url[: -len(suffix)]
    return url


def api_url(svc, url):
    return base_url(svc, url) + API_SUFFIX.get(svc, "") if url else ""


def default_rule(c):
    r = c.get("rules") or {"mode": "either", "min_seed_days": 14, "min_ratio": 2}
    return {"mode": r.get("mode") or r.get("require") or "either", "min_seed_days": r.get("min_seed_days"), "min_ratio": r.get("min_ratio")}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)
    with db() as con:
        con.execute("insert into log (ts, msg) values (?, ?)", (time.time(), msg))
        con.execute("delete from log where id <= (select max(id) from log) - 2000")
    if re.search(r"FAILED|failed|error:", msg):
        notify("Shredarr hit a problem", msg.strip(), "failure")


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u in ("GB", "TB") else f"{n:.0f} {u}"
        n /= 1024


# ---------- notifications ----------

NOTIFY_DEFAULTS = {"discord_url": "", "discord_username": "Shredarr", "discord_avatar": "",
                   "ntfy_server": "", "ntfy_topic": "", "ntfy_token": "", "ntfy_priority": "3",
                   "pushover_user": "", "pushover_token": "", "pushover_priority": "0", "pushover_sound": "",
                   "on_cleanup": True, "on_failure": True, "on_jellyfin": False, "on_undo": True,
                   "daily_summary": True, "summary_time": "09:00"}
NOTIFY_SWITCH = {"cleanup": "on_cleanup", "failure": "on_failure", "jellyfin": "on_jellyfin", "undo": "on_undo", "summary": "daily_summary"}


def notify_cfg():
    n = dict(cfg().get("notify", {}))
    if n.get("ntfy_url") and not n.get("ntfy_topic"):  # older single-URL setting
        u = urllib.parse.urlparse(n["ntfy_url"])
        n["ntfy_server"], n["ntfy_topic"] = f"{u.scheme}://{u.netloc}", u.path.strip("/")
    n.pop("ntfy_url", None)
    return {**NOTIFY_DEFAULTS, **{k: v for k, v in n.items() if k in NOTIFY_DEFAULTS}}


def notify(title, msg, kind):
    """Send in the background if the switch for this kind of event is on (kinds without a switch always send)."""
    try:
        n = notify_cfg()
    except Exception:
        return
    if kind in NOTIFY_SWITCH and not n.get(NOTIFY_SWITCH[kind]):
        return
    threading.Thread(target=send_notification, args=(n, title, msg), daemon=True).start()


def send_notification(n, title, msg):
    """Returns (services tried, errors). Errors are printed, never logged, so a broken service can't loop."""
    tried, errs = [], []
    def attempt(name, fn):
        tried.append(name)
        try:
            fn()
        except urllib.error.HTTPError as e:
            errs.append(f"{name}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
        except Exception as e:
            errs.append(f"{name}: {e}")
    if n["discord_url"]:
        payload = {"embeds": [{"title": title, "description": msg[:4000], "color": 0x3B82F6}]}
        if n["discord_username"]:
            payload["username"] = n["discord_username"]
        if n["discord_avatar"]:
            payload["avatar_url"] = n["discord_avatar"]
        attempt("Discord", lambda: call("POST", n["discord_url"], {"User-Agent": "Shredarr"}, payload))
    if n["ntfy_topic"]:
        hdr = {"Authorization": "Bearer " + n["ntfy_token"]} if n["ntfy_token"] else {}
        attempt("ntfy", lambda: call("POST", (n["ntfy_server"] or "https://ntfy.sh").rstrip("/"), hdr,
                                     {"topic": n["ntfy_topic"], "title": title, "message": msg, "tags": ["wastebasket"],
                                      "priority": int(n["ntfy_priority"] or 3)}))
    if n["pushover_user"] and n["pushover_token"]:
        f = {"token": n["pushover_token"], "user": n["pushover_user"], "title": title, "message": msg[:1024], "priority": n["pushover_priority"] or "0"}
        if n["pushover_sound"]:
            f["sound"] = n["pushover_sound"]
        if f["priority"] == "2":  # emergency priority requires retry/expire
            f.update(retry="60", expire="3600")
        attempt("Pushover", lambda: urllib.request.urlopen("https://api.pushover.net/1/messages.json", urllib.parse.urlencode(f).encode(), timeout=30).read())
    for e in errs:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), "[notify]", e, flush=True)
    return tried, errs


def ensure_discord_logo():
    """Once per webhook URL: with Avatar left blank, make the Shredarr logo the webhook's picture."""
    n, c = notify_cfg(), cfg()
    if not n["discord_url"] or n["discord_avatar"] or c.get("discord_logo_set") == n["discord_url"]:
        return
    try:
        discord_logo_avatar(n["discord_url"])
        c = cfg()
        c["discord_logo_set"] = n["discord_url"]
        save_cfg(c)
        log("Discord webhook picture set to the Shredarr logo")
    except Exception as e:
        log(f"Couldn't set the Discord webhook picture (failed: {e})")


def discord_logo_avatar(url):
    """With no avatar URL set, give the webhook itself the Shredarr logo. Discord can't fetch images from this
    private server, so the logo is uploaded to the webhook instead of being linked."""
    with open(os.path.join(STATIC, "logo.png"), "rb") as f:
        data = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    call("PATCH", url, {"User-Agent": "Shredarr"}, {"avatar": data})


def summary_text():
    since = time.time() - 86400
    with db() as con:
        n, size = con.execute("select count(*), coalesce(sum(size), 0) from cleaned where ts > ?", (since,)).fetchone()
        jf = con.execute("select count(*) from log where ts > ? and msg like 'Jellyfin deleted%'", (since,)).fetchone()[0]
        bad = con.execute("select count(*) from log where ts > ? and (msg like '%FAILED%' or msg like '%failed%')", (since,)).fetchone()[0]
        waiting = con.execute("select count(*) from pending").fetchone()[0]
    return (f"Last 24 hours: {n} torrent{'s' * (n != 1)} cleaned up ({human(size)} freed), "
            f"{jf} delete{'s' * (jf != 1)} from Jellyfin, {bad} problem{'s' * (bad != 1)}.\n"
            f"Waiting for cleanup: {waiting} torrent{'s' * (waiting != 1)}.")


def summarizer():
    sent = None
    while True:
        time.sleep(30)
        try:
            n, today = notify_cfg(), time.strftime("%Y-%m-%d")
            if n["daily_summary"] and time.strftime("%H:%M") == n["summary_time"] and sent != today:
                sent = today
                notify("Shredarr daily summary", summary_text(), "summary")
            with db() as con:
                con.execute("delete from sessions where expires < ?", (time.time(),))
        except Exception as e:
            print("summary:", e, flush=True)


# ---------- sign-in ----------

SESSION_DAYS = 30
_fails = {}


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240000).hex()}


def check_pw(pw):
    a = cfg().get("auth")
    return bool(a) and hmac.compare_digest(hash_pw(pw, a["salt"])["hash"], a["hash"])


POLICY_DEFAULTS = {"min_length": 8, "upper": False, "digit": False, "symbol": False}


def policy():
    return {**POLICY_DEFAULTS, **cfg().get("auth_policy", {})}


def policy_errors(pw):
    p, errs = policy(), []
    if len(pw) < max(1, int(p["min_length"])):
        errs.append(f"at least {max(1, int(p['min_length']))} character{'s' * (int(p['min_length']) > 1)}")
    if p["upper"] and not re.search(r"[A-Z]", pw):
        errs.append("an uppercase letter")
    if p["digit"] and not re.search(r"\d", pw):
        errs.append("a number")
    if p["symbol"] and not re.search(r"[^A-Za-z0-9]", pw):
        errs.append("a symbol")
    return errs


_reset = {"code": None, "expires": 0, "made": 0, "tries": 0}
RESET_FILE = os.path.join(os.path.dirname(CFG), "RESET-CODE.txt")
TORRENT_DIR = os.path.join(os.path.dirname(CFG), "torrents")  # .torrent files kept so a fully deleted torrent can be re-added


def make_reset_code():
    """One-time code, valid 15 minutes. Only someone who can read the server's files or the configured
    notification channels can see it."""
    if time.time() - _reset["made"] < 60:
        return False
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    _reset.update(code=code, expires=time.time() + 900, made=time.time(), tries=0)
    with open(RESET_FILE, "w") as f:
        f.write(f"Shredarr password reset code: {code}\nValid until {time.strftime('%H:%M', time.localtime(_reset['expires']))}.\n")
    os.chmod(RESET_FILE, 0o600)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[reset] password reset code: {code} (valid 15 minutes)", flush=True)
    log("Password reset code requested")
    n = notify_cfg()
    threading.Thread(target=send_notification, args=(n, "Shredarr password reset", f"Your reset code is {code}. It works for 15 minutes."), daemon=True).start()
    return True


def use_reset_code(code):
    if not _reset["code"] or time.time() > _reset["expires"] or _reset["tries"] >= 5:
        return False
    _reset["tries"] += 1
    if not hmac.compare_digest(code.strip().upper(), _reset["code"]):
        return False
    _reset.update(code=None, expires=0)
    try:
        os.remove(RESET_FILE)
    except FileNotFoundError:
        pass
    return True


def set_password(pw):
    c = cfg()
    c["auth"] = hash_pw(pw)
    save_cfg(c)


def new_session():
    tok = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("insert into sessions values (?,?,?)", (tok, time.time(), time.time() + SESSION_DAYS * 86400))
    return tok


def valid_session(tok):
    with db() as con:
        row = con.execute("select expires from sessions where token=?", (tok,)).fetchone()
    return bool(row) and row[0] > time.time()


# ---------- API clients ----------

def call(method, url, headers=None, body=None, opener=None):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with (opener or urllib.request.build_opener()).open(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"[", b"{") else raw.decode(errors="replace")


class Arr:
    """Sonarr/Radarr/Seerr; url includes the API prefix (/api/v3, /api/v1)."""
    def __init__(self, c):
        self.url, self.h = c["url"].rstrip("/"), {"X-Api-Key": c["api_key"]}

    def __call__(self, method, path, body=None, **q):
        return call(method, self.url + path + ("?" + urllib.parse.urlencode(q) if q else ""), self.h, body)


class QBit:
    def __init__(self, c):
        self.url = c["url"].rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        form = urllib.parse.urlencode({"username": c["username"], "password": c["password"]}).encode()
        self.op.open(urllib.request.Request(self.url + "/api/v2/auth/login", form, {"Referer": self.url}), timeout=30).read()

    def __call__(self, path, form=None, **q):
        url = self.url + "/api/v2" + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        with self.op.open(urllib.request.Request(url, data, {"Referer": self.url}), timeout=60) as r:
            raw = r.read()
        return json.loads(raw) if raw[:1] in (b"[", b"{") else raw

    def upload(self, path, fields, filename, content):
        boundary = "shredarr" + secrets.token_hex(8)
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="{filename}"\r\n'
                 "Content-Type: application/x-bittorrent\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/api/v2" + path, body, {"Referer": self.url, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self.op.open(req, timeout=60) as r:
            return r.read()


# ---------- Jellyfin delete handling ----------

def act(c, desc, fn):
    if c.get("dry_run"):
        return log(f"  [dry run] would have {desc}")
    try:
        fn()
        log(f"  {desc}")
    except Exception as e:
        log(f"  FAILED: {desc}: {e}")


def merge_undo(a, b):
    """Two episode deletes that share a season-pack torrent: undo should bring both back."""
    if not a:
        return b
    if a.get("kind") == b.get("kind") == "episodes" and a.get("series_id") == b.get("series_id"):
        a["remonitor"] = sorted(set(a["remonitor"]) | set(b["remonitor"]))
        a["seasons"] = sorted(set(a["seasons"]) | set(b["seasons"]))
    return a


def torrent_content(h):
    """Which movie or episodes a torrent was imported as, from Sonarr/Radarr history (season packs and complete series
    included). Saved before anything is removed, because Sonarr/Radarr drop the history when a movie or show is deleted."""
    imps = import_map(60).get(h, [])
    out = {"app": imps[0]["app"] if imps else None, "movieId": None, "seriesId": None, "episodes": []}
    ep_ids = {i["episodeId"] for i in imps if i.get("episodeId")}
    for i in imps:
        out["movieId"] = out["movieId"] or i.get("movieId")
        out["seriesId"] = out["seriesId"] or i.get("seriesId")
    if out["seriesId"] and ep_ids:
        try:
            eps = Arr(cfg()["sonarr"])("GET", "/episode", seriesId=out["seriesId"])
            out["episodes"] = sorted([e["seasonNumber"], e["episodeNumber"]] for e in eps if e["id"] in ep_ids)
        except Exception:
            pass
    return out


def queue_torrents(hashes, name, source, undo=None, event=None):
    new = 0
    with db() as con:
        for h in (x.lower() for x in hashes):
            row = con.execute("select undo from pending where hash=?", (h,)).fetchone()
            if row is None:
                try:
                    content = json.dumps(torrent_content(h))
                except Exception:
                    content = None
                con.execute("insert into pending (hash, name, source, added_at, status, undo, event, content) values (?,?,?,?,?,?,?,?)",
                            (h, name, source, time.time(), "queued", json.dumps(undo) if undo else None, event, content))
                new += 1
            elif undo:
                con.execute("update pending set undo=? where hash=?", (json.dumps(merge_undo(json.loads(row[0]) if row[0] else None, undo)), h))
    log(f"  {len(hashes)} torrent(s) found in history, {new} newly queued for cleanup after seeding")


def seerr_remove(c, kind, tmdb, title):
    if not tmdb or not c.get("seerr"):
        return
    s = Arr(c["seerr"])
    try:
        info = s("GET", f"/{kind}/{tmdb}").get("mediaInfo")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        info = None
    if not info:
        return log("  not in Seerr")
    act(c, f"removed '{title}' from Seerr", lambda: s("DELETE", f"/media/{info['id']}"))


def find_series(sonarr, tvdb=None, tmdb=None, imdb=None, title=None):
    series = sonarr("GET", "/series")
    for key, val in (("tvdbId", tvdb), ("tmdbId", tmdb), ("imdbId", imdb)):
        hits = [s for s in series if val and str(s.get(key)) == str(val)]
        if hits:
            return hits[0]
    hits = [s for s in series if title and s["title"].casefold() == title.casefold()]
    return hits[0] if len(hits) == 1 else None


def parent_series(c, sonarr, ev):
    ids = {}
    if ev.get("series_id"):
        jf = c["jellyfin"]
        try:
            r = call("GET", jf["url"].rstrip("/") + "/Items?" + urllib.parse.urlencode({"ids": ev["series_id"], "fields": "ProviderIds"}),
                     {"X-Emby-Token": jf["api_key"]})
            if r.get("Items"):
                ids = {k.lower(): v for k, v in (r["Items"][0].get("ProviderIds") or {}).items()}
        except Exception as e:
            log(f"  Jellyfin series lookup failed ({e}); matching by title")
    return find_series(sonarr, ids.get("tvdb"), ids.get("tmdb"), ids.get("imdb"), ev.get("series_name"))


def on_movie(c, ev):
    radarr = Arr(c["radarr"])
    movies = radarr("GET", "/movie")
    m = next((m for m in movies if ev.get("tmdb_id") and m.get("tmdbId") == ev["tmdb_id"]), None) \
        or next((m for m in movies if ev.get("imdb_id") and m.get("imdbId") == ev["imdb_id"]), None)
    if not m:
        hits = [m for m in movies if m["title"].casefold() == (ev.get("name") or "").casefold()]
        m = hits[0] if len(hits) == 1 else None
    if not m:
        return log("  no Radarr match")
    hashes = {h["downloadId"] for h in radarr("GET", "/history/movie", movieId=m["id"]) if h.get("downloadId")}
    snap = {"kind": "movie", "movie": {k: m.get(k) for k in ("tmdbId", "title", "year", "qualityProfileId", "path", "monitored", "minimumAvailability", "tags")}}
    queue_torrents(hashes, m["title"], "movie", snap, secrets.token_hex(6))
    bh = behavior()
    act(c, f"removed '{m['title']}' from Radarr" + (" (with import exclusion)" if bh["import_exclusion"] else ""),
        lambda: radarr("DELETE", f"/movie/{m['id']}", deleteFiles="true", addImportExclusion=str(bh["import_exclusion"]).lower()))
    if bh["remove_from_seerr"]:
        seerr_remove(c, "movie", m.get("tmdbId"), m["title"])


def on_series(c, ev):
    sonarr = Arr(c["sonarr"])
    s = find_series(sonarr, ev.get("tvdb_id"), ev.get("tmdb_id"), ev.get("imdb_id"), ev.get("name"))
    if not s:
        return log("  no Sonarr match")
    if os.path.isdir(s["path"]):
        # Deleting a show in Jellyfin deletes its folder. If the folder is still there, Jellyfin only dropped a show
        # with no episodes left on disk (e.g. after its last episode was deleted), so keep it in Sonarr for new episodes.
        return log(f"  '{s['title']}' folder still exists, so Jellyfin just dropped an empty show; keeping it in Sonarr")
    hashes = {h["downloadId"] for h in sonarr("GET", "/history/series", seriesId=s["id"]) if h.get("downloadId")}
    snap = {"kind": "series",
            "series": {k: s.get(k) for k in ("tvdbId", "title", "qualityProfileId", "path", "seasonFolder", "seriesType", "monitored", "monitorNewItems", "tags")},
            "seasons": [[x["seasonNumber"], x["monitored"]] for x in s["seasons"]],
            "monitored_eps": [[e["seasonNumber"], e["episodeNumber"]] for e in sonarr("GET", "/episode", seriesId=s["id"]) if e["monitored"]]}
    queue_torrents(hashes, s["title"], "series", snap, secrets.token_hex(6))
    bh = behavior()
    act(c, f"removed '{s['title']}' from Sonarr" + (" (with import list exclusion)" if bh["import_exclusion"] else ""),
        lambda: sonarr("DELETE", f"/series/{s['id']}", deleteFiles="true", addImportListExclusion=str(bh["import_exclusion"]).lower()))
    if bh["remove_from_seerr"]:
        seerr_remove(c, "tv", s.get("tmdbId"), s["title"])


def on_episodes(c, ev, season, numbers=None):
    """Whole season when numbers is None, otherwise the listed episode numbers."""
    sonarr = Arr(c["sonarr"])
    s = parent_series(c, sonarr, ev)
    if not s:
        return log("  no Sonarr match for the series")
    dirs = os.listdir(s["path"]) if os.path.isdir(s["path"]) else []
    season_dirs = [d for d in dirs if re.fullmatch(r"Season \d+|Specials", d, re.I)]
    if numbers is None and any(re.fullmatch(rf"Season 0*{season}" + ("|Specials" if season == 0 else ""), d, re.I) for d in season_dirs):
        # same idea as on_series: a deleted season loses its folder; an emptied one doesn't
        return log(f"  '{s['title']}' season {season} folder still exists, so Jellyfin just dropped an empty season; leaving it monitored")
    eps = [e for e in sonarr("GET", "/episode", seriesId=s["id"]) if e["seasonNumber"] == season]
    flat = numbers is None and not season_dirs  # episodes sit directly in the show folder
    if flat:
        # no season folders, so a deleted season can't be told apart from an emptied one: only unmonitor what has aired
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        eps = [e for e in eps if e.get("airDateUtc") and e["airDateUtc"] <= now]
    if numbers is not None:
        eps = [e for e in eps if ev.get("tvdb_id") and e.get("tvdbId") == ev["tvdb_id"]] or [e for e in eps if e["episodeNumber"] in numbers]
    if not eps:
        return log(f"  no matching episodes in Sonarr for '{s['title']}'")
    ids = sorted(e["id"] for e in eps)
    label = f"'{s['title']}' S{season:02d}" + ("" if numbers is None else "".join(f"E{e['episodeNumber']:02d}" for e in eps))
    hashes = {h["downloadId"] for h in sonarr("GET", "/history/series", seriesId=s["id"], seasonNumber=season)
              if h.get("downloadId") and h.get("episodeId") in ids}
    season_was = numbers is None and not flat and any(x["seasonNumber"] == season and x["monitored"] for x in s["seasons"])
    snap = {"kind": "episodes", "series_id": s["id"], "title": s["title"],
            "remonitor": [e["id"] for e in eps if e["monitored"]], "seasons": [season] if season_was else []}
    queue_torrents(hashes, label, "season" if numbers is None else "episode", snap, secrets.token_hex(6))
    bh = behavior()
    if bh["unmonitor"]:
        act(c, f"unmonitored {label} in Sonarr", lambda: sonarr("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": False}))
    if numbers is None and not flat and bh["unmonitor"]:
        def unmonitor_season():
            full = sonarr("GET", f"/series/{s['id']}")
            for x in full["seasons"]:
                if x["seasonNumber"] == season:
                    x["monitored"] = False
            sonarr("PUT", f"/series/{s['id']}", full)
        act(c, f"unmonitored season {season} of '{s['title']}'", unmonitor_season)
    files = sorted({e["episodeFileId"] for e in eps if e.get("episodeFileId")})
    if files and bh["remove_episode_files"]:
        act(c, f"removed {len(files)} episode file record(s) of {label} from Sonarr",
            lambda: sonarr("DELETE", "/episodefile/bulk", {"episodeFileIds": files}))


def handle(ev):
    if jf_id(ev.get("item_id")) in suppressed:  # deleted by Shredarr itself (Delete now); already handled
        return
    c = cfg()
    kind, sn, ep = ev.get("item_type"), ev.get("season_number"), ev.get("episode_number")
    who = " / ".join(x for x in (ev.get("series_name"), ev.get("name")) if x)
    log(f"Jellyfin deleted {kind}: {who}" + (f" (S{sn}" + (f"E{ep}" if ep is not None else "") + ")" if sn is not None else ""))
    if kind in ("Movie", "Series", "Season", "Episode"):
        notify("Deleted in Jellyfin", f"{kind}: {who}", "jellyfin")
    if kind == "Movie":
        on_movie(c, ev)
    elif kind == "Series":
        on_series(c, ev)
    elif kind == "Season" and sn is not None:
        on_episodes(c, ev, sn)
    elif kind == "Episode" and sn is not None and ep is not None:
        on_episodes(c, ev, sn, range(ep, (ev.get("episode_end_number") or ep) + 1))
    else:
        log("  ignored")


# ---------- torrent cleanup ----------

_qb = None
_lib_cache = {}


def qcall(path, form=None, **q):
    """qBittorrent call over a shared session, logging in again when it expires."""
    global _qb
    for attempt in (0, 1):
        try:
            if _qb is None:
                _qb = QBit(cfg()["qbittorrent"])
            return _qb(path, form, **q)
        except urllib.error.HTTPError as e:
            _qb = None
            if e.code != 403 or attempt:
                raise


_imports = {"t": 0, "map": {}}


def local(p):
    """A path as qBittorrent/Sonarr/Radarr report it -> the same file as this container sees it (Settings → Path mappings)."""
    for m in cfg().get("path_maps", []):
        if p and m.get("remote") and p.startswith(m["remote"]):
            return m["local"] + p[len(m["remote"]):]
    return p


def remote(p):
    """The reverse of local(): a path in this container -> the path Sonarr/Radarr know it by."""
    for m in cfg().get("path_maps", []):
        if p and m.get("local") and p.startswith(m["local"]):
            return m["remote"] + p[len(m["local"]):]
    return p


def torrents(**q):
    ts = qcall("/torrents/info", **q)
    for t in ts:
        t["content_path"], t["save_path"] = local(t["content_path"]), local(t["save_path"])
    return ts


_roots = {"t": 0, "dirs": []}


def library_path(p):
    """History keeps the path at import time. If a library root folder moved since (e.g. /media/Movies -> /data/Media/Movies),
    find the file under the current root folder of the same name."""
    p = local(p)
    if not p or os.path.exists(p):
        return p
    if time.time() - _roots["t"] > 300:
        c, dirs = cfg(), []
        for app in ("sonarr", "radarr"):
            try:
                dirs += [local(r["path"].rstrip("/")) for r in Arr(c[app])("GET", "/rootfolder")]
            except Exception:
                pass
        _roots.update(t=time.time(), dirs=dirs)
    for root in _roots["dirs"]:
        name = "/" + os.path.basename(root) + "/"
        i = p.find(name)
        if i >= 0:
            return root + p[i + len(name) - 1:]
    return p


def import_map(max_age=300):
    """torrent hash -> library imports recorded by Sonarr/Radarr ({app, path, movieId, seriesId, episodeId})."""
    if time.time() - _imports["t"] < max_age:
        return _imports["map"]
    c, m = cfg(), {}
    for app in ("sonarr", "radarr"):
        a, page = Arr(c[app]), 1
        while True:
            r = a("GET", "/history", page=page, pageSize=1000, eventType=3)  # 3 = downloadFolderImported
            for h in r["records"]:
                if h.get("downloadId"):
                    m.setdefault(h["downloadId"].lower(), []).append(
                        {"app": app, "path": library_path((h.get("data") or {}).get("importedPath")), "movieId": h.get("movieId"),
                         "seriesId": h.get("seriesId"), "episodeId": h.get("episodeId")})
            if page * 1000 >= r["totalRecords"]:
                break
            page += 1
    _imports.update(t=time.time(), map=m)
    return m


def in_library(t, max_age=0):
    """Library files that came from this torrent and still exist: hardlinked copies, or (for RAR releases,
    whose extracted copy isn't linked to the torrent) the paths Sonarr/Radarr imported it to."""
    hit = _lib_cache.get(t["hash"])
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    cp = t["content_path"]
    paths = [cp] if os.path.isfile(cp) else [os.path.join(d, f) for d, _, fs in os.walk(cp) for f in fs]
    linked = 0
    for p in paths:
        if p.lower().endswith(VIDEO) and "sample" not in os.path.basename(p).lower():
            try:
                linked += os.stat(p).st_nlink > 1
            except FileNotFoundError:
                pass
    imported = {i["path"] for i in import_map(max(max_age, 60)).get(t["hash"], []) if i["path"] and os.path.exists(i["path"])}
    n = max(linked, len(imported))
    _lib_cache[t["hash"]] = (time.time(), n)
    return n


def overrides(con):
    return {h: {"mode": m, "min_seed_days": d, "min_ratio": r}
            for h, m, d, r in con.execute("select hash, mode, days, ratio from overrides")}


_titles = {"t": 0, "series": {}, "movies": {}}


def titles():
    """Sonarr/Radarr id -> title, refreshed every 5 minutes (for per-show rules and labels)."""
    if time.time() - _titles["t"] > 300:
        c = cfg()
        try:
            _titles["series"] = {x["id"]: x["title"] for x in Arr(c["sonarr"])("GET", "/series")}
            _titles["movies"] = {x["id"]: x["title"] for x in Arr(c["radarr"])("GET", "/movie")}
        except Exception:
            pass
        _titles["t"] = time.time()
    return _titles


def title_of(h, pend_name=None):
    imps = import_map().get(h, [])
    tl = titles()
    for i in imps:
        if i["app"] == "sonarr" and i.get("seriesId") in tl["series"]:
            return tl["series"][i["seriesId"]]
        if i["app"] == "radarr" and i.get("movieId") in tl["movies"]:
            return tl["movies"][i["movieId"]]
    return re.sub(r"^'([^']+)'.*$", r"\1", pend_name) if pend_name else None


def tracker_host(t):
    """Only the tracker's host name. Never the full announce URL: it contains the user's private passkey."""
    host = urllib.parse.urlparse(t.get("tracker") or "").hostname or ""
    return host[4:] if host.startswith("www.") else host


def rule_for(t, ov, c, title=None):
    """The seeding rule for torrent t, and where it came from. First match wins:
    this torrent's own rule > show/movie rule > tracker rule > category rule > default."""
    if t["hash"] in ov:
        return {**ov[t["hash"]], "source": "this torrent"}
    extra = c.get("rule_list", [])
    host = tracker_host(t)
    for kind, test in (("title", lambda m: title and m.casefold() == title.casefold()),
                       ("tracker", lambda m: host and (host == m or host.endswith("." + m))),
                       ("category", lambda m: t.get("category") == m)):
        for r in extra:
            if r.get("type") == kind and r.get("match") and test(r["match"]):
                return {"mode": r["mode"], "min_seed_days": r.get("min_seed_days"), "min_ratio": r.get("min_ratio"), "source": r["match"]}
    return {**default_rule(c), "source": "default"}


def evaluate(r, t):
    """(progress text, rule met) for torrent t under rule r."""
    mode = r["mode"]
    if mode == "never":
        return "protected: never deleted", False
    days, ratio = t["seeding_time"] / 86400, t["ratio"]
    need = [(f"seeded {days:.1f} of {r['min_seed_days']:g} days", days >= r["min_seed_days"])] \
        if mode != "ratio" and r.get("min_seed_days") is not None else []
    need += [(f"ratio {ratio:.2f} of {r['min_ratio']:g}", ratio >= r["min_ratio"])] \
        if mode != "days" and r.get("min_ratio") is not None else []
    met = [ok for _, ok in need]
    done = t["progress"] >= 1 and (not met or (all(met) if mode == "both" else any(met)))
    return ", ".join(x for x, _ in need) or "no seeding rule", done


MOVIE_KEYS = ("tmdbId", "title", "year", "qualityProfileId", "path", "monitored", "minimumAvailability", "tags")
SERIES_KEYS = ("tvdbId", "title", "qualityProfileId", "path", "seasonFolder", "seriesType", "monitored", "monitorNewItems", "tags")


def capture_restore(t):
    """Right before a torrent is deleted for good: what it was (movie / show + episodes, with Sonarr/Radarr settings)
    and a copy of its .torrent file, so it can be downloaded again later from History."""
    h, c = t["hash"], cfg()
    with db() as con:
        row = con.execute("select undo, content from pending where hash=?", (h,)).fetchone()
    snap = json.loads(row[0]) if row and row[0] else {}
    content = json.loads(row[1]) if row and row[1] else None
    if not content:
        try:
            content = torrent_content(h)
        except Exception:
            content = {"episodes": []}
    out = {"hash": h, "name": t["name"], "category": t.get("category"), "episodes": content.get("episodes") or [], "movie": None, "series": None}
    try:
        if snap.get("kind") == "movie":
            out["movie"] = snap["movie"]
        elif content.get("movieId"):
            m = Arr(c["radarr"])("GET", f"/movie/{content['movieId']}")
            out["movie"] = {k: m.get(k) for k in MOVIE_KEYS}
        if snap.get("kind") == "series":
            out["series"] = {**snap["series"], "seasons": snap["seasons"]}
        else:
            sid = snap.get("series_id") or content.get("seriesId")
            if sid:
                sr = Arr(c["sonarr"])("GET", f"/series/{sid}")
                out["series"] = {**{k: sr.get(k) for k in SERIES_KEYS}, "seasons": [[x["seasonNumber"], x["monitored"]] for x in sr["seasons"]]}
    except Exception as e:
        print("capture_restore:", e, flush=True)
    try:
        data = qcall("/torrents/export", hash=h)
        if isinstance(data, bytes) and data.startswith(b"d"):
            os.makedirs(TORRENT_DIR, exist_ok=True)
            with open(os.path.join(TORRENT_DIR, h + ".torrent"), "wb") as f:
                f.write(data)
    except Exception as e:
        print("torrent export:", e, flush=True)
    return out


def record_cleanup(t, name, reason, restore=None):
    with db() as con:
        con.execute("insert into cleaned (ts, name, size, title, category, reason, hash, restore) values (?,?,?,?,?,?,?,?)",
                    (time.time(), t["name"], t["size"], title_of(t["hash"], name) or name, t.get("category"), reason, t["hash"],
                     json.dumps(restore) if restore else None))


def check(c, t, name, r, added=None):
    """Returns a status string, or None once the torrent is gone. r is the rule that applies."""
    if not t:
        log(f"torrent for {name} is gone from qBittorrent; dropping it from the queue")
        return None
    if t["progress"] < 1:
        return "still downloading"
    progress, met = evaluate(r, t)
    left = in_library(t)
    if left:
        return f"waiting: {left} file(s) still in library; {progress}"
    if not met:
        return f"seeding: {progress}"
    wait = (added or 0) + grace_seconds() - time.time()
    if added and wait > 0:
        return f"deletion delay: deletes in {duration(wait)} (Undo still possible); {progress}"
    if c.get("dry_run"):
        log(f"[dry run] would have deleted torrent '{t['name']}' and its data ({progress})")
        return f"[dry run] ready to delete; {progress}"
    restore = capture_restore(t)
    qcall("/torrents/delete", {"hashes": t["hash"], "deleteFiles": "true"})
    log(f"deleted torrent '{t['name']}' and its data ({progress})")
    record_cleanup(t, name, "seeding rule met", restore)
    notify("Torrent cleaned up", f"{name}\n{t['name']} · {human(t['size'])} freed ({progress})", "cleanup")
    return None


def sweep():
    with sweep_lock:
        c = cfg()
        if configured(c):
            return
        with db() as con:
            rows = con.execute("select hash, name, added_at from pending order by added_at").fetchall()
            ov = overrides(con)
        with db() as con:
            busy = con.execute("select count(*) from redownloads where done=0").fetchone()[0]
        if not rows and not ov and not busy:
            return
        ts = {t["hash"]: t for t in torrents()}
        if ts:  # forget rules of torrents that no longer exist
            with db() as con:
                con.executemany("delete from overrides where hash=?", [(h,) for h in ov if h not in ts])
        for h, name, added in rows:
            t = ts.get(h)
            try:
                status = check(c, t, name, rule_for(t, ov, c, title_of(h, name)) if t else None, added)
            except Exception as e:
                status = f"error: {e}"
            with db() as con:
                if status is None:
                    con.execute("delete from pending where hash=?", (h,))
                else:
                    con.execute("update pending set status=? where hash=?", (status, h))
        low_space_pass(c, ts, ov)
        try:
            redownload_pass()
        except Exception as e:
            log(f"redownload check failed: {e}")


def free_gb(ts):
    """Free space on the downloads disk, from the first torrent folder that exists."""
    for t in ts.values():
        if os.path.isdir(t["save_path"]):
            st = os.statvfs(t["save_path"])
            return st.f_bavail * st.f_frsize / 2**30
    return None


def low_space_pass(c, ts, ov):
    """Settings → Low disk space: below the minimum, waiting torrents go early (oldest first) even if their seeding rule
    isn't met yet. Torrents that are still in the library, protected, or inside their deletion delay are never touched."""
    ls = {**LOW_SPACE_DEFAULTS, **c.get("low_space", {})}
    if not ls["enabled"]:
        return
    free = free_gb(ts)
    if free is None or free >= ls["min_free_gb"]:
        return
    with db() as con:
        rows = con.execute("select hash, name, added_at from pending order by added_at").fetchall()
    for h, name, added in rows:
        t = ts.get(h)
        if not t or t["progress"] < 1 or in_library(t) or rule_for(t, ov, c)["mode"] == "never":
            continue
        if added + grace_seconds() > time.time():
            continue
        if c.get("dry_run"):
            log(f"[dry run] low disk space ({free:.0f} GB free): would have deleted '{t['name']}' early")
            continue
        log(f"Low disk space ({free:.0f} GB free, minimum {ls['min_free_gb']:g} GB): deleting '{t['name']}' early")
        restore = capture_restore(t)
        qcall("/torrents/delete", {"hashes": h, "deleteFiles": "true"})
        record_cleanup(t, name, "low disk space", restore)
        with db() as con:
            con.execute("delete from pending where hash=?", (h,))
        notify("Torrent cleaned up early", f"Low disk space: {t['name']} · {human(t['size'])} freed", "cleanup")
        time.sleep(3)
        free = free_gb(ts)
        if free is not None and free >= ls["min_free_gb"]:
            break


def safe_sweep():
    try:
        sweep()
    except Exception as e:
        log(f"sweep failed: {e}")


def worker():
    while True:
        ev = events.get()
        try:
            handle(ev)
        except Exception as e:
            log(f"  error: {e!r}")
        if events.empty():
            safe_sweep()


def sweeper():
    while True:
        safe_sweep()
        time.sleep(max(1, cfg().get("check_interval_minutes", 15)) * 60)


# ---------- Delete now ----------

def jellyfin(method, path, **q):
    jf = cfg()["jellyfin"]
    return call(method, jf["url"].rstrip("/") + path + ("?" + urllib.parse.urlencode(q) if q else ""), {"X-Emby-Token": jf["api_key"]})


def tail(path):
    """Last two path parts (folder/file). Matches the same file in Jellyfin and here even when mounted at different paths."""
    return "/".join(path.replace("\\", "/").rstrip("/").split("/")[-2:])


def jf_id(item_id):
    """Jellyfin writes item ids with dashes in webhooks and without them in its API."""
    return (item_id or "").replace("-", "").lower()


def jf_delete(item_id):
    suppressed.add(jf_id(item_id))
    jellyfin("DELETE", f"/Items/{item_id}")


def episodes_label(eps):
    out = []
    for sn in sorted({e["seasonNumber"] for e in eps}):
        nums = sorted(e["episodeNumber"] for e in eps if e["seasonNumber"] == sn)
        out.append(f"S{sn:02d}E{nums[0]:02d}" + (f"-E{nums[-1]:02d} ({len(nums)} episodes)" if len(nums) > 1 else ""))
    return ", ".join(out)


def seerr_step(c, kind, tmdb, title):
    if not tmdb or not c.get("seerr"):
        return None
    s = Arr(c["seerr"])
    try:
        info = s("GET", f"/{kind}/{tmdb}").get("mediaInfo")
    except urllib.error.HTTPError:
        info = None
    return (f"Remove '{title}' from Seerr", lambda: s("DELETE", f"/media/{info['id']}")) if info else None


def build_plan(c, h):
    """Everything 'Delete now' would remove for torrent h, as [(description, action)]."""
    steps = []
    t = (torrents(hashes=h) or [None])[0]
    imps = import_map(0).get(h, [])
    # Jellyfin: the library files this torrent was imported to (or, for unmanaged torrents like Courses, its own folder)
    targets = {tail(i["path"]) for i in imps if i["path"] and os.path.exists(i["path"])}
    if targets:
        items = jellyfin("GET", "/Items", recursive="true", fields="Path", includeItemTypes="Movie,Episode,Video",
                         enableImages="false", enableUserData="false")["Items"]
        hits = [i for i in items if tail(i.get("Path") or "") in targets]
        if hits:
            eps = [i for i in hits if i.get("Type") == "Episode"]
            labels = [f"'{n}' " + episodes_label([{"seasonNumber": i.get("ParentIndexNumber") or 0, "episodeNumber": i.get("IndexNumber") or 0}
                                                   for i in eps if i.get("SeriesName") == n]) for n in sorted({i.get("SeriesName") for i in eps})]
            labels += [f"'{i['Name']}'" for i in hits if i.get("Type") != "Episode"]
            steps.append((f"Delete {', '.join(labels)} from Jellyfin (and its library files)",
                          lambda hits=hits: [jf_delete(i["Id"]) for i in hits]))
    radarr, sonarr = Arr(c["radarr"]), Arr(c["sonarr"])
    for mid in sorted({i["movieId"] for i in imps if i["app"] == "radarr" and i["movieId"]}):
        try:
            m = radarr("GET", f"/movie/{mid}")
        except urllib.error.HTTPError:
            continue
        excl = behavior()["import_exclusion"]
        steps.append((f"Remove '{m['title']}' from Radarr, with its files" + (" (and add an import exclusion)" if excl else ""),
                      lambda m=m: radarr("DELETE", f"/movie/{m['id']}", deleteFiles="true", addImportExclusion=str(excl).lower())))
        if behavior()["remove_from_seerr"]:
            steps.append(seerr_step(c, "movie", m.get("tmdbId"), m["title"]))
    by_series = {}
    for i in imps:
        if i["app"] == "sonarr" and i["episodeId"]:
            by_series.setdefault(i["seriesId"], set()).add(i["episodeId"])
    for sid, epids in by_series.items():
        try:
            s = sonarr("GET", f"/series/{sid}")
        except urllib.error.HTTPError:
            continue
        eps = sonarr("GET", "/episode", seriesId=sid)
        mine = [e for e in eps if e["id"] in epids]
        label = f"'{s['title']}' {episodes_label(mine)}"
        # Episodes only: the series stays in Sonarr (and Seerr) so new episodes keep coming, even if none are left on disk.
        # Removing a whole show is done by deleting the series in Jellyfin.
        ids = sorted(e["id"] for e in mine)
        if behavior()["unmonitor"]:
            steps.append((f"Unmonitor {label} in Sonarr", lambda ids=ids: sonarr("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": False})))
        files = sorted({e["episodeFileId"] for e in mine if e.get("episodeFileId")})
        if files:
            steps.append((f"Remove {label} files from Sonarr",
                          lambda files=files: sonarr("DELETE", "/episodefile/bulk", {"episodeFileIds": files})))
    if t:
        steps.append((f"Delete torrent '{t['name']}' and its downloaded data from qBittorrent",
                      lambda: qcall("/torrents/delete", {"hashes": h, "deleteFiles": "true"})))
        if not imps:  # not managed by Sonarr/Radarr (e.g. Courses, which Jellyfin reads straight from the download folder)
            steps.append(("Rescan the Jellyfin library so it drops the deleted files", lambda: jellyfin("POST", "/Library/Refresh")))
    return t, [x for x in steps if x]


def delete_now(h):
    c = cfg()
    try:
        t, steps = build_plan(c, h)
    except Exception as e:
        return log(f"Delete now failed while planning: {e!r}")
    log(f"Delete now: {t['name'] if t else h}")
    restore = capture_restore(t) if t and not c.get("dry_run") else None
    for desc, fn in steps:
        if c.get("dry_run"):
            log(f"  [dry run] would: {desc}")
            continue
        try:
            fn()
            log(f"  done: {desc}")
        except Exception as e:
            log(f"  FAILED: {desc}: {e}")
    if not c.get("dry_run"):
        with db() as con:
            con.execute("delete from pending where hash=?", (h,))
            con.execute("delete from overrides where hash=?", (h,))
        if t:
            record_cleanup(t, t["name"], "Delete now", restore)
        _imports["t"] = 0
        if t:
            notify("Deleted now", f"{t['name']} · {human(t['size'])} freed", "cleanup")


# ---------- Undo ----------

def files_under(cp):
    return [cp] if os.path.isfile(cp) else [os.path.join(d, f) for d, _, fs in os.walk(cp) for f in fs]


def videos_in(paths):
    return [p for p in paths if p.lower().endswith(VIDEO) and "sample" not in os.path.basename(p).lower()]


def has_video(t):
    return bool(videos_in(files_under(t["content_path"])))


def has_archives(t):
    return any(ARCHIVE.search(p) for p in files_under(t["content_path"]))


def restorable(t):
    return "video" if has_video(t) else "rar" if has_archives(t) else None


def undo_info(h):
    """(torrent, [(hash, name, snapshot)], problem). A whole-series delete is undone as one, across all its torrents."""
    with db() as con:
        row = con.execute("select name, undo, event from pending where hash=?", (h,)).fetchone()
        if not row:
            return None, [], "it isn't waiting for cleanup"
        name, undo, event = row
        if not undo:
            return None, [], "it was queued before Undo existed"
        snap = json.loads(undo)
        rows = [(h, name, snap)]
        if snap["kind"] == "series" and event:
            rows += [(x, n, json.loads(u) if u else None)
                     for x, n, u in con.execute("select hash, name, undo from pending where event=? and hash!=?", (event, h))]
    t = (torrents(hashes=h) or [None])[0]
    if not t:
        return None, rows, "its torrent is already gone"
    how = restorable(t)
    if not how:
        return t, rows, "there's no video or RAR archive in the torrent to restore from"
    if how == "rar" and not restore_enabled():
        return t, rows, "it's a RAR release and RAR restore is turned off in Settings"
    return t, rows, None


def undo_steps(t, rows):
    snap, app = rows[0][2], "Radarr" if rows[0][2]["kind"] == "movie" else "Sonarr"
    if snap["kind"] == "movie":
        first = f"Re-add '{snap['movie']['title']}' to Radarr with its previous settings (if it was removed)"
    elif snap["kind"] == "series":
        first = f"Re-add '{snap['series']['title']}' to Sonarr with its previous settings and monitoring"
    else:
        first = f"Monitor {rows[0][1]} again in Sonarr"
    n = len(rows)
    middle = (f"Have {app} re-import the file{'s' if n > 1 else ''} from {'these torrents' if n > 1 else 'this torrent'}'s download, "
              "so it's back in your library and Jellyfin")
    if restorable(t) == "rar":
        middle = (f"Unpack it again from the torrent's RAR files (takes a minute or two), then have {app} import it, "
                  "so it's back in your library and Jellyfin")
    return [first, middle,
            f"Take {'these torrents' if n > 1 else 'the torrent'} off the cleanup list. "
            f"{'They keep' if n > 1 else 'It keeps'} seeding as normal"]


def wait_for(fn, timeout=120, every=3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def default_root(arr):
    return arr("GET", "/rootfolder")[0]["path"]


def default_profile(arr, items):
    """The quality profile used by most of the library, for items Shredarr only knows by name."""
    used = [x.get("qualityProfileId") for x in arr("GET", items)]
    return max(set(used), key=used.count) if used else arr("GET", "/qualityprofile")[0]["id"]


def ensure_movie(radarr, m):
    if radarr("GET", "/movie", tmdbId=m["tmdbId"]):
        return "already in Radarr"
    for e in radarr("GET", "/exclusions"):
        if e.get("tmdbId") == m["tmdbId"]:
            radarr("DELETE", f"/exclusions/{e['id']}")
    body = radarr("GET", "/movie/lookup/tmdb", tmdbId=m["tmdbId"])
    body.update({k: v for k, v in m.items() if v is not None})
    root = os.path.dirname(m["path"].rstrip("/")) if m.get("path") else default_root(radarr)
    body.update(rootFolderPath=root, addOptions={"searchForMovie": False, "monitor": "movieOnly"})
    if not body.get("qualityProfileId"):  # lookups return 0 for items not in the library yet
        body["qualityProfileId"] = default_profile(radarr, "/movie")
    radarr("POST", "/movie", body)
    return "re-added to Radarr"


def ensure_series(sonarr, snap):
    s = snap["series"]
    if any(x["tvdbId"] == s["tvdbId"] for x in sonarr("GET", "/series")):
        return "already in Sonarr"
    for e in sonarr("GET", "/importlistexclusion"):
        if e.get("tvdbId") == s["tvdbId"]:
            sonarr("DELETE", f"/importlistexclusion/{e['id']}")
    body = sonarr("GET", "/series/lookup", term=f"tvdb:{s['tvdbId']}")[0]
    body.update({k: v for k, v in s.items() if v is not None})
    root = os.path.dirname(s["path"].rstrip("/")) if s.get("path") else default_root(sonarr)
    body.update(rootFolderPath=root, addOptions={"monitor": "none", "searchForMissingEpisodes": False})
    if not body.get("qualityProfileId"):  # lookups return 0 for items not in the library yet
        body["qualityProfileId"] = default_profile(sonarr, "/series")
    sid = sonarr("POST", "/series", body)["id"]
    eps = wait_for(lambda: sonarr("GET", "/episode", seriesId=sid), timeout=90) or []
    full, want = sonarr("GET", f"/series/{sid}"), dict(map(tuple, snap["seasons"]))
    for x in full["seasons"]:
        x["monitored"] = bool(want.get(x["seasonNumber"], False))
    full["monitored"] = s.get("monitored", True)
    sonarr("PUT", f"/series/{sid}", full)
    on = {tuple(x) for x in snap["monitored_eps"]}
    ids = [e["id"] for e in eps if (e["seasonNumber"], e["episodeNumber"]) in on]
    if ids:
        sonarr("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": True})
    return "re-added to Sonarr with its monitoring restored"


def remonitor(sonarr, snap):
    if snap["remonitor"]:
        sonarr("PUT", "/episode/monitor", {"episodeIds": snap["remonitor"], "monitored": True})
    if snap["seasons"]:
        full = sonarr("GET", f"/series/{snap['series_id']}")
        for x in full["seasons"]:
            if x["seasonNumber"] in snap["seasons"]:
                x["monitored"] = True
        sonarr("PUT", f"/series/{snap['series_id']}", full)
    return f"monitored {len(snap['remonitor'])} episode(s) again"


def reimport(arr, kind, t, path=None):
    """Ask Sonarr/Radarr to import from the torrent's download again (hardlinked, never moved), then check it landed."""
    cmd = arr("POST", "/command", {"name": "DownloadedMoviesScan" if kind == "movie" else "DownloadedEpisodesScan",
                                   "path": remote(path or t["content_path"]), "downloadClientId": t["hash"].upper(), "importMode": "Copy"})
    done = wait_for(lambda: (lambda r: r if r["status"] in ("completed", "failed", "aborted", "cancelled") else None)(
        arr("GET", f"/command/{cmd['id']}")), timeout=300, every=4)
    _imports["t"] = 0
    _lib_cache.pop(t["hash"], None)
    n = wait_for(lambda: in_library(t), timeout=30, every=3)
    if not n:
        raise RuntimeError(f"nothing was imported ({(done or {}).get('status', 'timed out')}); see Activity → History in "
                           f"{'Radarr' if kind == 'movie' else 'Sonarr'} for the reason")
    jellyfin_rescan([i["path"] for i in import_map(0).get(t["hash"], []) if i["path"] and os.path.exists(i["path"])])
    return f"{n} file{'s' * (n != 1)} back in the library"


def jellyfin_rescan(paths, again=True):
    """See _jellyfin_rescan. Sent twice: Jellyfin can ignore the first notice when the file's folder is brand new."""
    _jellyfin_rescan(paths)
    if again:
        threading.Timer(90, _jellyfin_rescan, args=(paths,)).start()


def _jellyfin_rescan(paths):
    """Tell Jellyfin about restored files, for setups where Sonarr/Radarr don't notify it themselves.
    Jellyfin may see the library at another path, so map each file onto Jellyfin's own library folders by folder name."""
    try:
        locs = [l for f in jellyfin("GET", "/Library/VirtualFolders") for l in f.get("Locations", [])]
    except Exception as e:
        return print("jellyfin rescan:", e, flush=True)
    ups = []
    for p in paths:
        for loc in locs:
            name = "/" + os.path.basename(loc.rstrip("/")) + "/"
            i = p.find(name)
            if i >= 0:
                jp = loc.rstrip("/") + p[i + len(name) - 1:]
                ups += [{"Path": os.path.dirname(jp), "UpdateType": "Created"}, {"Path": jp, "UpdateType": "Created"}]
                break
    if ups:
        try:
            call("POST", cfg()["jellyfin"]["url"].rstrip("/") + "/Library/Media/Updated", {"X-Emby-Token": cfg()["jellyfin"]["api_key"]}, {"Updates": ups})
        except Exception as e:
            print("jellyfin rescan:", e, flush=True)


def unpack_restore(arr, kind, t):
    """RAR release: unpack its archives (read in place, never changed) into a temporary folder beside the downloads,
    have Sonarr/Radarr import the video from there, then remove the temporary folder."""
    if not restore_enabled():
        raise RuntimeError("restoring RAR releases is turned off in Settings")
    if not shutil.which("unar"):
        raise RuntimeError("the unpacker (unar) isn't installed in this container")
    src = t["content_path"]
    base = src if os.path.isdir(src) else os.path.dirname(src)
    st = os.stat(base)
    out_dir = os.path.join(os.path.dirname(base), "_shredarr-restore", re.sub(r"[^\w.\- ]", "_", t["name"]))
    shutil.rmtree(out_dir, ignore_errors=True)
    try:
        firsts = [f for f in files_under(src) if re.search(r"\.rar$", f, re.I) and not re.search(r"\.part0*[2-9]\d*\.rar$", f, re.I)]
        if not firsts:
            raise RuntimeError("couldn't find the first .rar volume in the torrent")
        for f in firsts:
            dest = os.path.join(out_dir, os.path.relpath(os.path.dirname(f), base))
            os.makedirs(dest, exist_ok=True)
            r = subprocess.run(["unar", "-q", "-f", "-D", "-o", dest, f], capture_output=True, text=True, timeout=3600)
            if r.returncode:
                raise RuntimeError("couldn't unpack it: " + (r.stderr or r.stdout).strip()[-300:])
        if not videos_in(files_under(out_dir)):
            raise RuntimeError("the archive didn't contain a video file")
        for d, _, fs in os.walk(os.path.dirname(out_dir)):  # hand the files to the same owner as the downloads
            os.chown(d, st.st_uid, st.st_gid)
            for f in fs:
                os.chown(os.path.join(d, f), st.st_uid, st.st_gid)
        return "unpacked, " + reimport(arr, kind, t, out_dir)
    finally:
        time.sleep(5)
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(out_dir))  # only if no other restore is using it
        except OSError:
            pass


def restore_check():
    """Settings → Check setup: can this container restore a RAR release?"""
    msgs = []
    if not restore_enabled():
        return True, ["Restoring RAR releases is turned off."]
    if not shutil.which("unar"):
        return False, ["The unpacker (unar) isn't installed in this container. Use the Shredarr image."]
    msgs.append("The unpacker (unar) is installed.")
    dirs = sorted({os.path.dirname(t["save_path"].rstrip("/") + "/x") for t in torrents() if os.path.isdir(t["save_path"])})
    for d in dirs:
        probe = os.path.join(d, ".shredarr-probe")
        try:
            with open(probe, "w") as f:
                f.write("probe")
            os.remove(probe)
        except OSError as e:
            return False, msgs + [f"Shredarr can't write to {d} ({e.strerror}). Mount your downloads read-write."]
    msgs.append(f"Shredarr can write next to your downloads ({len(dirs)} folder{'s' * (len(dirs) != 1)}).")
    return True, msgs


def do_undo(h):
    c = cfg()
    try:
        t, rows, problem = undo_info(h)
    except Exception as e:
        t, rows, problem = None, [], repr(e)
    if problem:
        return log(f"Undo failed: {problem}")
    snap = rows[0][2]
    kind = "movie" if snap["kind"] == "movie" else "tv"
    arr = Arr(c["radarr"] if kind == "movie" else c["sonarr"])
    log(f"Undo: {rows[0][1]}")
    def step(fn):
        try:
            log(f"  done: {fn()}")
            return True
        except Exception as e:
            log(f"  FAILED: {e}")
            return False
    if snap["kind"] == "movie":
        step(lambda: ensure_movie(arr, snap["movie"]))
    elif snap["kind"] == "series":
        step(lambda: ensure_series(arr, snap))
    else:
        step(lambda: remonitor(arr, snap))
    with db() as con:  # off the cleanup list first, so a sweep can't delete them while they're being restored
        con.executemany("delete from pending where hash=?", [(x,) for x, _, _ in rows])
    log(f"  done: took {len(rows)} torrent{'s' * (len(rows) != 1)} off the cleanup list")
    ok = True
    for x, name, _ in rows:
        tt = t if x == h else (torrents(hashes=x) or [None])[0]
        how = tt and restorable(tt)
        if how == "video":
            ok &= step(lambda tt=tt: f"re-imported '{tt['name']}': " + reimport(arr, kind, tt))
        elif how == "rar":
            log(f"  unpacking '{tt['name']}'; this can take a minute or two")
            ok &= step(lambda tt=tt: f"restored '{tt['name']}': " + unpack_restore(arr, kind, tt))
    if c.get("seerr"):
        try:
            Arr(c["seerr"])("POST", f"/settings/jobs/{'radarr' if kind == 'movie' else 'sonarr'}-scan/run")
        except Exception:
            pass
    notify("Undo " + ("finished" if ok else "had a problem"), rows[0][1], "undo")


def torrents_json():
    c = cfg()
    if configured(c):
        return {"setup": configured(c), "torrents": [], "default": default_rule(c), "dry_run": bool(c.get("dry_run")), "modes": MODES,
                "health": health, "version": VERSION, "update": update_info}
    with db() as con:
        ov = overrides(con)
        pend = {h: (n, s, u, a) for h, n, s, u, a in con.execute("select hash, name, status, undo, added_at from pending")}
        freed = con.execute("select coalesce(sum(size), 0), count(*) from cleaned").fetchone()
        freed30 = con.execute("select coalesce(sum(size), 0) from cleaned where ts > ?", (time.time() - 30 * 86400,)).fetchone()[0]
    imap = import_map()
    grace = grace_seconds()
    out, raw = [], torrents()
    for t in raw:
        h = t["hash"]
        name = pend[h][0] if h in pend else None
        title = title_of(h, name)
        r = rule_for(t, ov, c, title)
        progress, met = evaluate(r, t)
        lib = in_library(t, max_age=60)
        out.append({"hash": h, "name": t["name"], "title": title, "category": t["category"], "tracker": tracker_host(t),
                    "state": t["state"], "progress": t["progress"],
                    "ratio": t["ratio"], "seeding_days": t["seeding_time"] / 86400, "upspeed": t["upspeed"], "dlspeed": t["dlspeed"],
                    "size": t["size"], "added_on": t["added_on"], "rule": {k: r[k] for k in ("mode", "min_seed_days", "min_ratio")},
                    "rule_source": r["source"], "custom": h in ov, "rule_progress": progress,
                    "rule_met": met, "tracked": h in imap,
                    "days_met": t["seeding_time"] / 86400 >= r["min_seed_days"]
                    if r["mode"] not in ("ratio", "never") and r.get("min_seed_days") is not None else None,
                    "ratio_met": t["ratio"] >= r["min_ratio"] if r["mode"] not in ("days", "never") and r.get("min_ratio") is not None else None,
                    "in_library": lib, "leftover": h in imap and not lib and h not in pend and t["progress"] >= 1,
                    "queued": h in pend, "queued_as": name, "status": pend[h][1] if h in pend else None,
                    "delete_after": pend[h][3] + grace if h in pend and grace else None,
                    "undo": None if h not in pend else ("ok" if pend[h][2] and restorable(t) else "no")})
    return {"default": default_rule(c), "dry_run": bool(c.get("dry_run")), "modes": MODES, "torrents": out,
            "freed": {"total": freed[0], "count": freed[1], "last30": freed30}, "health": health,
            "version": VERSION, "update": update_info, "free_gb": free_gb({t["hash"]: t for t in raw})}


# ---------- connections, health, updates ----------

health = {}
update_info = {}
WEBHOOK_TEMPLATE = ("ewogICJub3RpZmljYXRpb25fdHlwZSI6ICJ7e2pzb25fZW5jb2RlIE5vdGlmaWNhdGlvblR5cGV9fSIsCiAgIml0ZW1fdHlwZSI6ICJ7e2pzb25fZW5jb2RlIEl0ZW1UeXBlfX0iLAogICJpdGVtX2lkIjogInt7anNvbl9lbmNvZGUgSXRlbUlkfX0iLAogICJuYW1lIjogInt7anNvbl9lbmNvZGUgTmFtZX19IiwKICAicGF0aCI6IG51bGwsCiAgInRtZGJfaWQiOiB7eyNpZl9leGlzdCBQcm92aWRlcl90bWRifX17e1Byb3ZpZGVyX3RtZGJ9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgInR2ZGJfaWQiOiB7eyNpZl9leGlzdCBQcm92aWRlcl90dmRifX17e1Byb3ZpZGVyX3R2ZGJ9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgImltZGJfaWQiOiB7eyNpZl9leGlzdCBQcm92aWRlcl9pbWRifX0ie3tqc29uX2VuY29kZSBQcm92aWRlcl9pbWRifX0ie3tlbHNlfX1udWxse3svaWZfZXhpc3R9fSwKICAic2VyaWVzX25hbWUiOiB7eyNpZl9leGlzdCBTZXJpZXNOYW1lfX0ie3tqc29uX2VuY29kZSBTZXJpZXNOYW1lfX0ie3tlbHNlfX1udWxse3svaWZfZXhpc3R9fSwKICAic2VyaWVzX2lkIjoge3sjaWZfZXhpc3QgU2VyaWVzSWR9fSJ7e2pzb25fZW5jb2RlIFNlcmllc0lkfX0ie3tlbHNlfX1udWxse3svaWZfZXhpc3R9fSwKICAic2Vhc29uX251bWJlciI6IHt7I2lmX2V4aXN0IFNlYXNvbk51bWJlcn19e3tTZWFzb25OdW1iZXJ9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgImVwaXNvZGVfbnVtYmVyIjoge3sjaWZfZXhpc3QgRXBpc29kZU51bWJlcn19e3tFcGlzb2RlTnVtYmVyfX17e2Vsc2V9fW51bGx7ey9pZl9leGlzdH19LAogICJlcGlzb2RlX2VuZF9udW1iZXIiOiB7eyNpZl9leGlzdCBFcGlzb2RlTnVtYmVyRW5kfX17e0VwaXNvZGVOdW1iZXJFbmR9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgIm9jY3VycmVkX2F0IjogInt7anNvbl9lbmNvZGUgVXRjVGltZXN0YW1wfX0iCn0=")


def test_service(svc, conf):
    """Can Shredarr reach this service with these settings? Returns a short description, or raises."""
    url = (conf or {}).get("url", "").strip().rstrip("/")
    if not url:
        raise RuntimeError("no address set")
    if svc in ("sonarr", "radarr"):
        r = Arr({"url": api_url(svc, url), "api_key": conf.get("api_key", "")})("GET", "/system/status")
        return f"{r.get('appName', SERVICES[svc])} {r.get('version', '')}".strip()
    if svc == "seerr":
        seerr = Arr({"url": api_url(svc, url), "api_key": conf.get("api_key", "")})
        seerr("GET", "/settings/main")  # needs a valid API key
        return "Seerr " + (seerr("GET", "/status").get("version") or "")
    if svc == "qbittorrent":
        q = QBit({"url": url, "username": conf.get("username", ""), "password": conf.get("password", "")})
        v = q("/app/version")
        if not v or b"Forbidden" in (v if isinstance(v, bytes) else b""):
            raise RuntimeError("wrong username or password")
        return "qBittorrent " + (v.decode() if isinstance(v, bytes) else str(v))
    if svc == "jellyfin":
        r = call("GET", url + "/System/Info", {"X-Emby-Token": conf.get("api_key", "")})
        return f"Jellyfin {r.get('Version', '')} · {r.get('ServerName', '')}"
    raise RuntimeError("unknown service")


def check_health():
    c = cfg()
    for svc in SERVICES:
        conf = c.get(svc)
        if not conf or not conf.get("url"):
            health.pop(svc, None)
            continue
        was = health.get(svc, {}).get("ok")
        try:
            info = test_service(svc, {**conf, "url": base_url(svc, conf["url"])})
            health[svc] = {"ok": True, "msg": info, "ts": time.time()}
            if was is False:
                log(f"Connection restored: {SERVICES[svc]}")
        except Exception as e:
            msg = f"HTTP {e.code}" + (" (check the API key)" if e.code in (401, 403) else "") if isinstance(e, urllib.error.HTTPError) else str(getattr(e, "reason", e))
            health[svc] = {"ok": False, "msg": msg, "ts": time.time()}
            if was is not False:
                log(f"Connection FAILED: can't reach {SERVICES[svc]} ({msg})")


def check_update():
    if not cfg().get("update_check", True):
        update_info.clear()
        return
    try:
        r = call("GET", f"https://api.github.com/repos/{REPO}/releases/latest", {"User-Agent": "Shredarr", "Accept": "application/vnd.github+json"})
        latest = (r.get("tag_name") or "").lstrip("v")
        newer = tuple(int(x) for x in re.findall(r"\d+", latest)[:3]) > tuple(int(x) for x in re.findall(r"\d+", VERSION)[:3])
        update_info.clear()
        update_info.update(latest=latest, url=r.get("html_url"), newer=newer)
    except Exception as e:
        print("update check:", e, flush=True)


def monitor():
    n = 0
    while True:
        try:
            check_health()
            if n % 144 == 0:  # every 12 hours
                check_update()
        except Exception as e:
            print("monitor:", e, flush=True)
        n += 1
        time.sleep(300)


def setup_jellyfin_webhook(shredarr_url):
    """Create or update the Webhook plugin entry that sends Jellyfin deletes to Shredarr."""
    c = cfg()
    if not c.get("webhook_token"):
        c["webhook_token"] = secrets.token_hex(32)
        save_cfg(c)
    plugins = jellyfin("GET", "/Plugins")
    pl = next((x for x in plugins if "webhook" in x["Name"].lower()), None)
    if not pl:
        raise RuntimeError("the Webhook plugin isn't installed in Jellyfin. Install it from Dashboard → Plugins → Catalog, "
                           "restart Jellyfin, then try again")
    conf = jellyfin("GET", f"/Plugins/{pl['Id']}/Configuration")
    opts = conf.setdefault("GenericOptions", [])
    target = shredarr_url.rstrip("/") + "/webhook/jellyfin"
    mine = next((o for o in opts if o.get("WebhookUri", "").endswith("/webhook/jellyfin")), None)
    if not mine:
        mine = {}
        opts.append(mine)
    mine.update({"NotificationTypes": ["ItemDeleted"], "WebhookName": "Shredarr", "WebhookUri": target, "EnableMovies": True,
                 "EnableEpisodes": True, "EnableSeries": True, "EnableSeasons": True, "EnableAlbums": False, "EnableSongs": False,
                 "EnableVideos": True, "SendAllProperties": False, "TrimWhitespace": False, "SkipEmptyMessageBody": False,
                 "EnableWebhook": True, "Template": WEBHOOK_TEMPLATE, "UserFilter": [],
                 "Headers": [{"Key": "X-Webhook-Token", "Value": c["webhook_token"]}], "Fields": []})
    jf = c["jellyfin"]
    call("POST", jf["url"].rstrip("/") + f"/Plugins/{pl['Id']}/Configuration", {"X-Emby-Token": jf["api_key"]}, conf)
    log(f"Jellyfin webhook set up: deletes are sent to {target}")
    return target


def history(q=""):
    with db() as con:
        rows = con.execute("select rowid, ts, name, size, title, category, reason, hash, restore, redl_ts, redl_how "
                           "from cleaned order by ts desc limit 1000").fetchall()
    q = q.casefold()
    out = []
    for r in rows:
        if q and q not in (r[2] or "").casefold() and q not in (r[4] or "").casefold():
            continue
        out.append({"id": r[0], "ts": r[1], "name": r[2], "size": r[3], "title": r[4], "category": r[5], "reason": r[6],
                    "redownloaded": r[9], "redownload_how": r[10],
                    "saved_torrent": bool(r[7]) and os.path.isfile(os.path.join(TORRENT_DIR, (r[7] or "") + ".torrent"))})
    return out


def episodes_text(eps):
    eps = [tuple(e) for e in eps]
    if not eps:
        return ""
    return episodes_label([{"seasonNumber": s_, "episodeNumber": e_} for s_, e_ in eps])


def resolve_redownload(row_id):
    """What a History entry was, as {app, movie, series, episodes, name, category, hash}. Entries saved before
    Shredarr kept this are worked out from the torrent name with Sonarr/Radarr's own parser."""
    with db() as con:
        r = con.execute("select name, category, hash, restore, title from cleaned where rowid=?", (row_id,)).fetchone()
    if not r:
        raise RuntimeError("that History entry doesn't exist")
    name, category, h, restore, title = r
    if restore:
        info = json.loads(restore)
        if info.get("movie") or info.get("series"):
            info["app"] = "radarr" if info.get("movie") else "sonarr"
            return info
    c, info = cfg(), {"hash": h, "name": name, "category": category, "movie": None, "series": None, "episodes": []}
    order = ("sonarr", "radarr") if re.search(r"tv|sonarr|show|series", category or "", re.I) else ("radarr", "sonarr")
    for app in order:
        try:
            arr = Arr(c[app])
            pr = arr("GET", "/parse", title=name)
        except Exception:
            continue
        if app == "radarr" and (pr.get("movie") or (pr.get("parsedMovieInfo") or {}).get("movieTitles")):
            m = pr.get("movie")
            if not m:
                pm = pr["parsedMovieInfo"]
                found = arr("GET", "/movie/lookup", term=f"{pm['movieTitles'][0]} {pm.get('year') or ''}".strip())
                m = found[0] if found else None
            if m:
                info.update(app="radarr", movie={"tmdbId": m["tmdbId"], "title": m["title"], "year": m.get("year"), "monitored": True,
                                                  "minimumAvailability": m.get("minimumAvailability") or "released"})
                return info
        if app == "sonarr" and pr.get("parsedEpisodeInfo"):
            pe = pr["parsedEpisodeInfo"]
            sr = pr.get("series")
            if not sr:
                found = arr("GET", "/series/lookup", term=pe.get("seriesTitle") or title or name)
                sr = found[0] if found else None
            if sr:
                eps = [[pe["seasonNumber"], n] for n in pe.get("episodeNumbers") or []]
                info.update(app="sonarr", series={"tvdbId": sr["tvdbId"], "title": sr["title"], "monitored": True, "seasonFolder": True,
                                                   "seasons": []},
                            episodes=eps, full_seasons=[pe["seasonNumber"]] if pe.get("fullSeason") else [])
                return info
    raise RuntimeError("Shredarr couldn't tell which movie or show this was")


def redownload_plan(row_id):
    info = resolve_redownload(row_id)
    saved = bool(info.get("hash")) and os.path.isfile(os.path.join(TORRENT_DIR, info["hash"] + ".torrent"))
    if info["app"] == "radarr":
        what = f"'{info['movie']['title']}'" + (f" ({info['movie']['year']})" if info["movie"].get("year") else "")
        first = f"Put {what} back in Radarr (with its previous settings if Shredarr has them) and monitor it"
        search = "Have Radarr search for it and grab the best release it finds"
    else:
        eps = episodes_text(info.get("episodes") or [])
        full = info.get("full_seasons") or []
        what = f"'{info['series']['title']}'" + (f" {eps}" if eps else (f" season {', '.join(map(str, full))}" if full else ""))
        first = f"Put '{info['series']['title']}' back in Sonarr if it was removed, and monitor {eps or 'those episodes'}"
        search = "Have Sonarr search for " + (eps or "them") + " (whole seasons as season packs)"
    return {"what": what, "app": info["app"], "same_available": saved,
            "steps": {"same": [first, f"Add the original torrent back to qBittorrent ({info['name']}), so the same release downloads again",
                               "When it finishes, it's imported into your library and shows up in Jellyfin"],
                      "search": [first, search, "When it finishes, it's imported into your library and shows up in Jellyfin"]}}


def do_redownload(row_id, how):
    c = cfg()
    try:
        info = resolve_redownload(row_id)
    except Exception as e:
        return log(f"Redownload failed: {e}")
    title = info["movie"]["title"] if info["app"] == "radarr" else info["series"]["title"]
    log(f"Redownload: {title}" + (f" {episodes_text(info.get('episodes') or [])}" if info["app"] == "sonarr" else ""))
    arr = Arr(c[info["app"]])
    try:
        if info["app"] == "radarr":
            log("  done: " + ensure_movie(arr, info["movie"]))
            m = arr("GET", "/movie", tmdbId=info["movie"]["tmdbId"])[0]
            if not m["monitored"]:
                m["monitored"] = True
                arr("PUT", f"/movie/{m['id']}", m)
            target = {"movieIds": [m["id"]]}
        else:
            sn = info["series"]
            snap = {"series": {k: v for k, v in sn.items() if k != "seasons"}, "seasons": sn.get("seasons") or [], "monitored_eps": []}
            log("  done: " + ensure_series(arr, snap))
            sr = next(x for x in arr("GET", "/series") if x["tvdbId"] == sn["tvdbId"])
            eps = wait_for(lambda: arr("GET", "/episode", seriesId=sr["id"]), timeout=90) or []
            want = {tuple(e) for e in info.get("episodes") or []}
            full = set(info.get("full_seasons") or [])
            ids = [e["id"] for e in eps if (e["seasonNumber"], e["episodeNumber"]) in want or e["seasonNumber"] in full]
            if ids:
                arr("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": True})
            seasons = sorted({e["seasonNumber"] for e in eps if e["id"] in ids})
            whole = [n for n in seasons if all(e["id"] in ids for e in eps if e["seasonNumber"] == n and e.get("airDateUtc"))]
            full_series = seasons and set(whole) == {e["seasonNumber"] for e in eps if e["seasonNumber"] > 0 and e.get("airDateUtc")}
            if whole:
                fresh = arr("GET", f"/series/{sr['id']}")
                for x in fresh["seasons"]:
                    if x["seasonNumber"] in whole:
                        x["monitored"] = True
                arr("PUT", f"/series/{sr['id']}", fresh)
            log(f"  done: monitored {len(ids)} episode{'s' * (len(ids) != 1)} in Sonarr")
            target = {"seriesId": sr["id"], "episodeIds": ids, "whole": whole, "full_series": full_series}
        if how == "same":
            path = os.path.join(TORRENT_DIR, (info.get("hash") or "") + ".torrent")
            with open(path, "rb") as f:
                content = f.read()
            global _qb
            if _qb is None:
                _qb = QBit(c["qbittorrent"])
            _qb.upload("/torrents/add", {"category": info.get("category") or ""}, os.path.basename(path), content)
            with db() as con:
                con.execute("insert or replace into redownloads (hash, ts, app, title, tries, last_try, done) values (?,?,?,?,0,0,0)",
                            (info["hash"], time.time(), info["app"], title))
            log(f"  done: added the original torrent back to qBittorrent ({info['name']})")
        else:
            if info["app"] == "radarr":
                arr("POST", "/command", {"name": "MoviesSearch", "movieIds": target["movieIds"]})
                log("  done: asked Radarr to search for it")
            elif target["full_series"]:
                arr("POST", "/command", {"name": "SeriesSearch", "seriesId": target["seriesId"]})
                log("  done: asked Sonarr to search for the whole series")
            else:
                for n in target["whole"]:
                    arr("POST", "/command", {"name": "SeasonSearch", "seriesId": target["seriesId"], "seasonNumber": n})
                rest = [i for i in target["episodeIds"] if i not in {e["id"] for e in arr("GET", "/episode", seriesId=target["seriesId"]) if e["seasonNumber"] in target["whole"]}]
                if rest:
                    arr("POST", "/command", {"name": "EpisodeSearch", "episodeIds": rest})
                log("  done: asked Sonarr to search for " + (f"season {', '.join(map(str, target['whole']))}" if target["whole"] else "")
                    + (" and " if target["whole"] and rest else "") + (f"{len(rest)} episode{'s' * (len(rest) != 1)}" if rest else ""))
        if c.get("seerr"):
            try:
                Arr(c["seerr"])("POST", f"/settings/jobs/{info['app']}-scan/run")
            except Exception:
                pass
        with db() as con:
            con.execute("update cleaned set redl_ts=?, redl_how=? where rowid=?", (time.time(), how, row_id))
        notify("Redownload started", f"{title}: " + ("re-adding the original torrent" if how == "same" else "searching for a release"), "undo")
    except Exception as e:
        log(f"  FAILED: redownload of {title}: {e}")


def redownload_pass():
    """Re-added torrents are normally imported by Sonarr/Radarr on their own. If one has finished but still isn't in
    the library after 10 minutes, nudge the import (or unpack a RAR release first)."""
    with db() as con:
        rows = con.execute("select hash, ts, app, title, tries, last_try from redownloads where done=0").fetchall()
    for h, ts, app, title, tries, last in rows:
        t = (torrents(hashes=h) or [None])[0]
        if not t:
            if time.time() - ts > 3600:
                with db() as con:
                    con.execute("update redownloads set done=1 where hash=?", (h,))
            continue
        _lib_cache.pop(h, None)
        _imports["t"] = 0
        if in_library(t):
            with db() as con:
                con.execute("update redownloads set done=1 where hash=?", (h,))
            jellyfin_rescan([i["path"] for i in import_map(0).get(h, []) if i["path"] and os.path.exists(i["path"])])
            log(f"Redownload finished: '{title}' is back in your library")
            notify("Redownload finished", f"{title} is back in your library", "undo")
            continue
        if t["progress"] < 1 or time.time() - max(ts, last or 0) < 600 or tries >= 3:
            continue
        with db() as con:
            con.execute("update redownloads set tries=tries+1, last_try=? where hash=?", (time.time(), h))
        arr, kind = Arr(cfg()[app]), "movie" if app == "radarr" else "tv"
        try:
            how = unpack_restore(arr, kind, t) if restorable(t) == "rar" else reimport(arr, kind, t)
            log(f"Redownload of '{title}' imported: {how}")
        except Exception as e:
            log(f"Redownload of '{title}': import not done yet ({e})")


# ---------- web ----------



def mode_options(selected):
    return "".join(f"<option value='{k}'{' selected' if k == selected else ''}>{html.escape(v)}</option>" for k, v in MODES.items())


def rule_text(r):
    if r.get("mode") == "never":
        return "never delete"
    return _rule_text(r)


def _rule_text(r):
    p = ([f"{r['min_seed_days']:g} days"] if r["mode"] != "ratio" and r.get("min_seed_days") is not None else []) + \
        ([f"ratio {r['min_ratio']:g}"] if r["mode"] != "days" and r.get("min_ratio") is not None else [])
    return (" and " if r["mode"] == "both" else " or ").join(p) or "no rule"


def page():
    c = cfg()
    r = default_rule(c)
    fmt = lambda v: "" if v is None else f"{v:g}"
    dry = bool(c.get("dry_run"))
    sub = {"%BANNERHIDDEN%": "" if dry else "hidden", "%MODECLS%": "dry" if dry else "live", "%MODETXT%": "Dry run" if dry else "Live", "%DAYS%": fmt(r["min_seed_days"]),
           "%RATIO%": fmt(r["min_ratio"]), "%MODEOPTS%": mode_options(r["mode"]), "%DRYCHK%": "checked" if dry else "",
           "%MODESJSON%": json.dumps(MODES)}
    sub["%VERSION%"] = VERSION
    sub["%ASSETV%"] = f"{VERSION}.{int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC)))}"
    out = read_static("index.html")
    for k, v in sub.items():
        out = out.replace(k, v)
    return out



SETUP_BODY = """%ERR%<p class=small style="text-align:center;margin-bottom:14px">Welcome! Choose a password to protect Shredarr.
You'll connect Sonarr, Radarr, qBittorrent and Jellyfin next.</p>
<form method=post action=/setup><label>Password<input type=password name=new autocomplete=new-password autofocus required></label>
<label>Confirm password<input type=password name=confirm autocomplete=new-password required></label><button>Create password</button></form>"""

LOGIN_BODY = """<form method=post action=/login>%ERR%<label>Password<input type=password name=password autocomplete=current-password autofocus required></label>
<button>Sign in</button></form><div class=links><a href=/reset>Forgot password?</a></div>"""

RESET_BODY = """%ERR%<form method=post action=/reset/code><button class=ghost>Send me a reset code</button></form>
<p class=small>The code goes to your notification services (if any are set up). It's also saved as
<code>RESET-CODE.txt</code> in Shredarr's config folder and shown in the container log. It works for 15 minutes.</p><hr>
<form method=post action=/reset><label>Reset code<input name=code autocomplete=one-time-code placeholder="XXXX-XXXX-XXXX" required></label>
<label>New password<input type=password name=new autocomplete=new-password required></label>
<label>Confirm new password<input type=password name=confirm autocomplete=new-password required></label>
<button>Reset password</button></form><div class=links><a href=/login>Back to sign in</a></div>"""


class Handler(BaseHTTPRequestHandler):
    PUBLIC = {"/login", "/reset", "/setup", "/logo.svg", "/logo.png", "/favicon.ico", "/health"}

    def reply(self, code, text, ctype="text/plain", headers=()):
        b = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, where, headers=()):
        self.send_response(303)
        self.send_header("Location", where)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def back(self):
        self.redirect("/")

    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def session(self):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return c["shredarr_session"].value if "shredarr_session" in c else None

    def authed(self):
        if self.client_address[0] == "127.0.0.1":  # scripts inside the container
            return True
        if not cfg().get("auth"):
            return False
        tok = self.session()
        return bool(tok) and valid_session(tok)

    def cookie_header(self, tok, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return ("Set-Cookie", f"shredarr_session={tok}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}")

    def deny(self):
        if self.path.startswith("/api/") or self.headers.get("X-Fetch") or self.command == "POST":
            return self.reply(401, "sign in required")
        self.redirect("/login" if cfg().get("auth") else "/setup")

    def setup_page(self, err="", code=200):
        body = SETUP_BODY.replace("%ERR%", f"<div class=err>{html.escape(err)}</div>" if err else "")
        self.reply(code, read_static("login.html").replace("%SUB%", "First-time setup").replace("%BODY%", body), "text/html")

    def login_page(self, err="", code=200):
        body = LOGIN_BODY.replace("%ERR%", f"<div class=err>{html.escape(err)}</div>" if err else "")
        self.reply(code, read_static("login.html").replace("%SUB%", "Sign in to manage cleanups").replace("%BODY%", body), "text/html")

    def reset_page(self, msg="", ok=False, code=200):
        note = f"<div class={'ok' if ok else 'err'}>{html.escape(msg)}</div>" if msg else ""
        self.reply(code, read_static("login.html").replace("%SUB%", "Reset your password").replace("%BODY%", RESET_BODY.replace("%ERR%", note)), "text/html")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        aliases = {"/logo.svg": "logo.svg", "/logo.png": "logo.png", "/favicon.ico": "logo.png"}
        name = aliases.get(path) or (path[len("/static/"):] if path.startswith("/static/") else None)
        if name and "/" not in name and name != "index.html" and os.path.isfile(os.path.join(STATIC, name)):
            ctype = CONTENT_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
            with open(os.path.join(STATIC, name), "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return self.wfile.write(b)
        if path == "/health":
            return self.reply(200, "ok")
        if path == "/login":
            if not cfg().get("auth"):
                return self.redirect("/setup")
            return self.back() if self.authed() else self.login_page()
        if path == "/reset":
            return self.reset_page() if cfg().get("auth") else self.redirect("/setup")
        if path == "/setup":
            return self.redirect("/") if cfg().get("auth") else self.setup_page()
        if not self.authed():
            return self.deny()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/api/torrents":
            try:
                return self.reply(200, json.dumps(torrents_json()), "application/json")
            except Exception as e:
                return self.reply(502, f"qBittorrent: {e}")
        if path == "/api/plan":
            h = query.get("hash", [""])[0]
            try:
                t, steps = build_plan(cfg(), h)
            except Exception as e:
                return self.reply(502, f"could not build the plan: {e}")
            return self.reply(200, json.dumps({"name": t["name"] if t else h, "steps": [d for d, _ in steps],
                                               "dry_run": bool(cfg().get("dry_run"))}), "application/json")
        if path == "/api/undo-plan":
            h = query.get("hash", [""])[0]
            try:
                t, rows, problem = undo_info(h)
            except Exception as e:
                return self.reply(502, f"could not check: {e}")
            return self.reply(200, json.dumps({"name": rows[0][1] if rows else h, "problem": problem, "count": len(rows),
                                               "steps": [] if problem else undo_steps(t, rows)}), "application/json")
        if path == "/api/redownload-plan":
            try:
                return self.reply(200, json.dumps(redownload_plan(int(query.get("id", ["0"])[0]))), "application/json")
            except Exception as e:
                return self.reply(200, json.dumps({"problem": str(e)}), "application/json")
        if path == "/api/history":
            rows = history(query.get("q", [""])[0])
            return self.reply(200, json.dumps({"rows": rows, "total": sum(r["size"] or 0 for r in rows)}), "application/json")
        if path == "/api/health":
            return self.reply(200, json.dumps(health), "application/json")
        if path == "/api/log":
            with db() as con:
                return self.reply(200, json.dumps(con.execute("select ts, msg from log order by id desc limit 500").fetchall()), "application/json")
        if path == "/api/settings":
            g, c = cfg().get("grace", {}), cfg()
            conns = {svc: {k: ("" if k in SECRET_KEYS else (base_url(svc, v) if k == "url" else v)) for k, v in (c.get(svc) or {}).items()}
                     | {"has_secret": any((c.get(svc) or {}).get(k) for k in SECRET_KEYS)} for svc in SERVICES}
            try:
                ts = torrents() if not configured(c) else []
            except Exception:
                ts = []
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(":")[0]
            guess = f"http://{host or 'shredarr'}:8089"
            try:  # prefer the address Jellyfin's webhook already uses
                pl = next(x for x in jellyfin("GET", "/Plugins") if "webhook" in x["Name"].lower())
                uri = next(o["WebhookUri"] for o in jellyfin("GET", f"/Plugins/{pl['Id']}/Configuration").get("GenericOptions", [])
                           if o.get("WebhookUri", "").endswith("/webhook/jellyfin"))
                guess = uri[: -len("/webhook/jellyfin")]
            except Exception:
                pass
            return self.reply(200, json.dumps({"connections": conns, "health": health, "missing": configured(c),
                                               "behavior": behavior(), "rule_list": c.get("rule_list", []),
                                               "low_space": {**LOW_SPACE_DEFAULTS, **c.get("low_space", {})},
                                               "path_maps": c.get("path_maps", []), "update_check": c.get("update_check", True),
                                               "version": VERSION, "update": update_info, "repo": REPO,
                                               "webhook_url_guess": guess,
                                               "choices": {"categories": sorted({t["category"] for t in ts if t["category"]}),
                                                           "trackers": sorted({tracker_host(t) for t in ts} - {""}),
                                                           "titles": sorted(set(titles()["series"].values()) | set(titles()["movies"].values()), key=str.casefold) if ts else []},
                                               "free_gb": free_gb({t["hash"]: t for t in ts}),
                                               "notify": notify_cfg(), "policy": policy(), "restore": {"enabled": restore_enabled()},
                                               "grace": {"value": g.get("value") or 0, "unit": g.get("unit") or "hours"},
                                               "unar": bool(shutil.which("unar"))}), "application/json")
        if path not in ("/", "/settings"):
            return self.reply(404, "not found")
        self.reply(200, page(), "text/html")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/webhook/jellyfin"):  # Jellyfin authenticates with its own token, not a session
            token = cfg().get("webhook_token")
            if token and self.headers.get("X-Webhook-Token") != token:
                return self.reply(401, "bad token")
            try:
                events.put(json.loads(body))
            except ValueError:
                return self.reply(400, "bad json")
            return self.reply(200, "queued")
        if path == "/setup":
            if cfg().get("auth"):  # only before a password exists
                return self.reply(403, "already set up")
            f = urllib.parse.parse_qs(body.decode())
            g = lambda k: f.get(k, [""])[0]
            if g("new") != g("confirm"):
                return self.setup_page("The passwords don't match.", 400)
            if errs := policy_errors(g("new")):
                return self.setup_page("The password needs " + ", ".join(errs) + ".", 400)
            set_password(g("new"))
            log("Password created during first-time setup")
            return self.redirect("/settings", [self.cookie_header(new_session(), SESSION_DAYS * 86400)])
        if path == "/reset/code":
            if make_reset_code():
                return self.reset_page("Code sent. Check your notifications, or the file on the server.", ok=True)
            return self.reset_page("A code was just made. Wait a minute before asking for another.", code=429)
        if path == "/reset":
            f = urllib.parse.parse_qs(body.decode())
            g = lambda k: f.get(k, [""])[0]
            if g("new") != g("confirm"):
                return self.reset_page("The new passwords don't match.", code=400)
            if errs := policy_errors(g("new")):
                return self.reset_page("The new password needs " + ", ".join(errs) + ".", code=400)
            if not use_reset_code(g("code")):
                time.sleep(1)
                return self.reset_page("That code isn't valid or has expired. Ask for a new one.", code=400)
            set_password(g("new"))
            with db() as con:
                con.execute("delete from sessions")
            log("Password reset with a reset code (all devices were signed out)")
            return self.redirect("/", [self.cookie_header(new_session(), SESSION_DAYS * 86400)])
        if path == "/login":
            ip, now = self.client_ip(), time.time()
            _fails[ip] = [x for x in _fails.get(ip, []) if now - x < 900]
            if len(_fails[ip]) >= 10:
                return self.login_page("Too many attempts. Try again in 15 minutes.", 429)
            pw = urllib.parse.parse_qs(body.decode()).get("password", [""])[0]
            if not check_pw(pw):
                _fails[ip].append(now)
                time.sleep(1)
                return self.login_page("That password isn't right.", 401)
            _fails.pop(ip, None)
            return self.redirect("/", [self.cookie_header(new_session(), SESSION_DAYS * 86400)])
        if not self.authed():
            return self.deny()
        if path == "/logout":
            with db() as con:
                con.execute("delete from sessions where token=?", (self.session(),))
            return self.redirect("/login", [self.cookie_header("", 0)])
        if path == "/api/logout-all":
            with db() as con:
                con.execute("delete from sessions")
            log("Signed out everywhere")
            return self.reply(200, "ok", headers=[self.cookie_header("", 0)])
        if path == "/api/password":
            f = json.loads(body)
            if not check_pw(f.get("current", "")):
                return self.reply(400, "Your current password isn't right.")
            if errs := policy_errors(f.get("new", "")):
                return self.reply(400, "The new password needs " + ", ".join(errs) + ".")
            set_password(f["new"])
            with db() as con:
                con.execute("delete from sessions where token != ?", (self.session() or "",))
            log("Password changed (other devices were signed out)")
            return self.reply(200, "ok")
        if path == "/api/settings":
            n = json.loads(body).get("notify", {})
            c = cfg()
            old = notify_cfg()
            c["notify"] = {k: (bool(n[k]) if isinstance(v, bool) else str(n[k]).strip()) for k, v in NOTIFY_DEFAULTS.items() if k in n}
            if not re.fullmatch(r"\d\d:\d\d", c["notify"].get("summary_time", "09:00")):
                return self.reply(400, "Summary time should look like 09:00.")
            new = {**NOTIFY_DEFAULTS, **c["notify"]}
            if new["discord_url"] and not re.match(r"https://(discord|discordapp)\.com/api/webhooks/", new["discord_url"]):
                return self.reply(400, "That doesn't look like a Discord webhook URL.")
            save_cfg(c)
            log("Notification settings saved")
            ensure_discord_logo()
            return self.reply(200, "ok")
        if path == "/api/connections":
            f, c = json.loads(body), cfg()
            for svc, conf in f.items():
                if svc not in SERVICES:
                    continue
                old = c.get(svc) or {}
                new = {k: str(v).strip() for k, v in conf.items() if k in ("url", "api_key", "username", "password")}
                if not new.get("url"):
                    if svc == "seerr":
                        c.pop(svc, None)
                    continue
                for k in SECRET_KEYS:
                    if k in new and not new[k] and old.get(k):
                        new[k] = old[k]  # blank = keep the saved key/password
                new["url"] = api_url(svc, new["url"])
                c[svc] = new
            save_cfg(c)
            global _qb
            _qb = None
            _imports["t"] = _titles["t"] = 0
            log("Connection settings saved")
            threading.Thread(target=check_health, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/test-connection":
            f, c = json.loads(body), cfg()
            svc, conf = f.get("service"), dict(f.get("conf") or {})
            for k in SECRET_KEYS:
                if not conf.get(k) and (c.get(svc) or {}).get(k):
                    conf[k] = c[svc][k]
            try:
                return self.reply(200, json.dumps({"ok": True, "msg": test_service(svc, conf)}), "application/json")
            except urllib.error.HTTPError as e:
                msg = f"HTTP {e.code}" + (": check the API key" if e.code in (401, 403) else "")
            except Exception as e:
                msg = str(getattr(e, "reason", e))
            return self.reply(200, json.dumps({"ok": False, "msg": msg}), "application/json")
        if path == "/api/jellyfin-webhook":
            try:
                target = setup_jellyfin_webhook(json.loads(body).get("url", ""))
                return self.reply(200, "Jellyfin will now send deletes to " + target)
            except Exception as e:
                return self.reply(400, f"Couldn't set up the webhook: {e}")
        if path == "/api/behavior":
            f, c = json.loads(body), cfg()
            c["behavior"] = {k: bool(f.get(k, v)) for k, v in BEHAVIOR_DEFAULTS.items()}
            save_cfg(c)
            log("Cleanup behavior saved: " + ", ".join(f"{k.replace('_', ' ')} {'on' if v else 'off'}" for k, v in c["behavior"].items()))
            return self.reply(200, "ok")
        if path == "/api/rule-list":
            rules = []
            for r in json.loads(body).get("rules", []):
                if r.get("type") not in ("category", "title", "tracker") or not str(r.get("match", "")).strip() or r.get("mode") not in MODES:
                    continue
                num = lambda k: float(r[k]) if r.get(k) not in (None, "") else None
                rules.append({"type": r["type"], "match": str(r["match"]).strip(), "mode": r["mode"],
                              "min_seed_days": num("min_seed_days"), "min_ratio": num("min_ratio")})
            c = cfg()
            c["rule_list"] = rules
            save_cfg(c)
            log(f"Seeding rules saved: {len(rules)} rule{'s' * (len(rules) != 1)}")
            threading.Thread(target=safe_sweep, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/low-space":
            f, c = json.loads(body), cfg()
            try:
                c["low_space"] = {"enabled": bool(f.get("enabled")), "min_free_gb": max(1.0, float(f.get("min_free_gb") or 100))}
            except ValueError:
                return self.reply(400, "Minimum free space should be a number.")
            save_cfg(c)
            log(f"Low disk space override {'on' if c['low_space']['enabled'] else 'off'} (minimum {c['low_space']['min_free_gb']:g} GB free)")
            threading.Thread(target=safe_sweep, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/path-maps":
            maps = [{"remote": m["remote"].strip().rstrip("/") + "/", "local": m["local"].strip().rstrip("/") + "/"}
                    for m in json.loads(body).get("maps", []) if m.get("remote", "").strip() and m.get("local", "").strip()]
            c = cfg()
            c["path_maps"] = maps
            save_cfg(c)
            _imports["t"] = _roots["t"] = 0
            log(f"Path mappings saved: {len(maps)}")
            return self.reply(200, "ok")
        if path == "/api/update-check":
            c = cfg()
            c["update_check"] = bool(json.loads(body).get("enabled"))
            save_cfg(c)
            threading.Thread(target=check_update, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/redownload":
            f = json.loads(body)
            threading.Thread(target=do_redownload, args=(int(f.get("id", 0)), "same" if f.get("how") == "same" else "search"), daemon=True).start()
            return self.reply(202, "started")
        if path == "/api/queue":
            hs = [h for h in json.loads(body).get("hashes", []) if re.fullmatch(r"[0-9a-f]{40}", h)]
            ts = {t["hash"]: t for t in torrents()}
            for h in hs:
                if h in ts:
                    queue_torrents([h], title_of(h) or ts[h]["name"], "leftover")
            log(f"Added {len(hs)} leftover torrent{'s' * (len(hs) != 1)} to the cleanup list")
            threading.Thread(target=safe_sweep, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/grace":
            f = json.loads(body)
            try:
                value = max(0.0, min(8760.0, float(f.get("value") or 0)))
            except ValueError:
                return self.reply(400, "The delay should be a number.")
            unit = "days" if f.get("unit") == "days" else "hours"
            c = cfg()
            c["grace"] = {"value": value, "unit": unit}
            save_cfg(c)
            log("Deletion delay saved: " + (f"{value:g} {unit[:-1] if value == 1 else unit}" if value else "none (delete as soon as the rule is met)"))
            threading.Thread(target=safe_sweep, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/restore":
            c = cfg()
            c["restore"] = {"enabled": bool(json.loads(body).get("enabled"))}
            save_cfg(c)
            log("Restoring RAR releases with Undo: " + ("on" if c["restore"]["enabled"] else "off"))
            return self.reply(200, "ok")
        if path == "/api/restore-check":
            ok, msgs = restore_check()
            return self.reply(200 if ok else 400, json.dumps({"ok": ok, "messages": msgs}), "application/json")
        if path == "/api/password-policy":
            f = json.loads(body)
            try:
                p = {"min_length": max(1, min(128, int(f.get("min_length") or 1))), "upper": bool(f.get("upper")),
                     "digit": bool(f.get("digit")), "symbol": bool(f.get("symbol"))}
            except ValueError:
                return self.reply(400, "Minimum length should be a number.")
            c = cfg()
            c["auth_policy"] = p
            save_cfg(c)
            log("Password rules saved: " + (", ".join([f"at least {p['min_length']} characters"] + [n for k, n in
                (("upper", "uppercase"), ("digit", "number"), ("symbol", "symbol")) if p[k]]) if p != {"min_length": 1, "upper": False, "digit": False, "symbol": False} else "no requirements"))
            return self.reply(200, "ok")
        if path == "/api/notify-test":
            ensure_discord_logo()
            tried, errs = send_notification(notify_cfg(), "Shredarr test", "Notifications are working. 🗑️")
            if not tried:
                return self.reply(400, "Add at least one service first, and save.")
            return self.reply(502 if errs else 200, "; ".join(errs) or "Sent to " + ", ".join(tried))
        if path in ("/api/undo", "/api/delete-now"):
            f = json.loads(body)
            hs = f.get("hashes") or [f.get("hash", "")]
            fn = do_undo if path == "/api/undo" else delete_now
            threading.Thread(target=lambda: [fn(h) for h in hs], daemon=True).start()
            return self.reply(202, "started")
        if path == "/api/rule":
            f = json.loads(body)
            hs, mode = f.get("hashes") or [f.get("hash", "")], f.get("mode") or ""
            num = lambda k: float(f[k]) if f.get(k) is not None else None
            with db() as con:
                for h in hs:
                    if mode in MODES:
                        con.execute("insert or replace into overrides values (?,?,?,?)", (h, mode, num("min_seed_days"), num("min_ratio")))
                    else:
                        con.execute("delete from overrides where hash=?", (h,))
            what = rule_text({"mode": mode, "min_seed_days": num("min_seed_days"), "min_ratio": num("min_ratio")}) + f" ({MODES[mode]})" if mode in MODES else "back to default"
            if len(hs) == 1:
                name = next((t["name"] for t in torrents(hashes=hs[0])), hs[0])
                log(f"rule for '{name}': {what}")
            else:
                log(f"rule for {len(hs)} torrents: {what}")
            threading.Thread(target=safe_sweep, daemon=True).start()
            return self.reply(200, "ok")
        f = urllib.parse.parse_qs(body.decode())
        num = lambda k: float(f[k][0]) if f.get(k, [""])[0].strip() else None
        if path == "/rules":
            mode = f.get("mode", ["either"])[0]
            c = cfg()
            c["rules"] = {"mode": mode if mode in MODES else "either", "min_seed_days": num("min_seed_days"), "min_ratio": num("min_ratio")}
            c["dry_run"] = "dry_run" in f
            save_cfg(c)
            log(f"Default rule saved: {rule_text(c['rules'])}, {MODES[c['rules']['mode']]} · " + ("dry run on" if c["dry_run"] else "live"))
            threading.Thread(target=safe_sweep, daemon=True).start()
            return self.reply(200, "ok") if self.headers.get("X-Fetch") else self.back()
        if path == "/sweep":
            safe_sweep()
            return self.reply(200, "ok") if self.headers.get("X-Fetch") else self.back()
        self.reply(404, "not found")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["set-password"]:  # docker exec shredarr python3 /app/shredarr.py set-password 'new password'
        set_password(sys.argv[2])
        sys.exit(print("password set"))
    db().close()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=sweeper, daemon=True).start()
    threading.Thread(target=summarizer, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    if not cfg().get("webhook_token"):
        c0 = cfg()
        c0["webhook_token"] = secrets.token_hex(32)
        save_cfg(c0)
    log("Shredarr started")
    ThreadingHTTPServer(("0.0.0.0", 8089), Handler).serve_forever()
