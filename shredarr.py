#!/usr/bin/env python3
"""Shredarr: when something is deleted in Jellyfin or Plex, remove it from Sonarr/Radarr/Seerr
right away, then delete its torrent (and download copy) from the torrent client once the
seeding rules are met and none of the torrent's files are still in the library."""
import base64, hashlib, hmac, html, http.cookiejar, http.cookies, json, os, queue, re, secrets, shutil, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.2.0"
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
CLIENTS = {"qbittorrent": "qBittorrent", "transmission": "Transmission", "deluge": "Deluge"}
MEDIA_SERVERS = {"jellyfin": "Jellyfin", "plex": "Plex"}
SERVICES = {"sonarr": "Sonarr", "radarr": "Radarr", "seerr": "Seerr", **CLIENTS, **MEDIA_SERVERS, "prowlarr": "Prowlarr"}
ARR_KINDS = ("sonarr", "radarr")
OPTIONAL = ("seerr", "prowlarr")
SECRET_KEYS = ("api_key", "password")  # never sent back to the browser
API_SUFFIX = {"sonarr": "/api/v3", "radarr": "/api/v3", "seerr": "/api/v1", "prowlarr": "/api/v1"}
BEHAVIOR_DEFAULTS = {"import_exclusion": True, "remove_from_seerr": True, "unmonitor": True, "remove_episode_files": True,
                     "cross_seed_with_original": False}
LOW_SPACE_DEFAULTS = {"enabled": False, "min_free_gb": 100}


def behavior():
    return {**BEHAVIOR_DEFAULTS, **cfg().get("behavior", {})}


def client_kind(c=None):
    """Which torrent client is used: qbittorrent, transmission or deluge."""
    k = (c if c is not None else cfg()).get("torrent_client")
    return k if k in CLIENTS else "qbittorrent"


def media_kind(c=None):
    """Which media server is used: jellyfin or plex."""
    k = (c if c is not None else cfg()).get("media_server")
    return k if k in MEDIA_SERVERS else "jellyfin"


def client_name():
    return CLIENTS[client_kind()]


def media_name():
    return MEDIA_SERVERS[media_kind()]


def required(c=None):
    c = c if c is not None else cfg()
    return ["sonarr", "radarr", client_kind(c), media_kind(c)]


def configured(c=None):
    """The required connections that aren't set up yet."""
    c = c if c is not None else cfg()
    return [k for k in required(c) if not (c.get(k) or {}).get("url")]


def arr_instances(kind=None, c=None):
    """[(key, kind, name, conf)] for every Sonarr/Radarr: the main ones (keys "sonarr" and "radarr") first, then any
    extra instances such as a 4K Radarr (keys like "radarr-4k")."""
    c = c if c is not None else cfg()
    out = [(k, k, SERVICES[k], c[k]) for k in ARR_KINDS if (c.get(k) or {}).get("url")]
    out += [(x["key"], x["kind"], x.get("name") or x["key"], x) for x in c.get("extra_arrs", []) if x.get("url") and x.get("key")]
    return [x for x in out if not kind or x[1] == kind]


def kind_of(key):
    """'sonarr' or 'radarr' for an instance key ("radarr", "radarr-4k", ...)."""
    return (key or "").split("-")[0]


def arr_api(key):
    """API client for a Sonarr/Radarr instance by key. Falls back to the main instance of that kind if the instance
    was removed from Settings since."""
    insts = arr_instances()
    conf = next((x[3] for x in insts if x[0] == key), None) or next((x[3] for x in insts if x[0] == kind_of(key)), None)
    if not conf:
        raise RuntimeError(f"{SERVICES.get(kind_of(key), key)} isn't set up")
    return Arr(conf)


def arr_name(key):
    return next((x[2] for x in arr_instances() if x[0] == key), SERVICES.get(kind_of(key), key))


def base_url(svc, url):
    """What the user typed (http://sonarr:8989) -> what the API client needs (…/api/v3)."""
    url = (url or "").strip().rstrip("/")
    suffix = API_SUFFIX.get(svc) or API_SUFFIX.get(kind_of(svc), "")
    if suffix and url.endswith(suffix):
        url = url[: -len(suffix)]
    return url


def api_url(svc, url):
    return base_url(svc, url) + (API_SUFFIX.get(svc) or API_SUFFIX.get(kind_of(svc), "")) if url else ""


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
        jf = con.execute("select count(*) from log where ts > ? and (msg like 'Jellyfin deleted%' or msg like 'Plex deleted%')",
                         (since,)).fetchone()[0]
        bad = con.execute("select count(*) from log where ts > ? and (msg like '%FAILED%' or msg like '%failed%')", (since,)).fetchone()[0]
        waiting = con.execute("select count(*) from pending").fetchone()[0]
    return (f"Last 24 hours: {n} torrent{'s' * (n != 1)} cleaned up ({human(size)} freed), "
            f"{jf} delete{'s' * (jf != 1)} from {media_name()}, {bad} problem{'s' * (bad != 1)}.\n"
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


class QBitClient:
    def __init__(self, conf):
        self.q = QBit(conf)

    def torrents(self, hashes=None):
        return self.q("/torrents/info", **({"hashes": "|".join(hashes)} if hashes else {}))

    def remove(self, hashes, with_data):
        self.q("/torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "true" if with_data else "false"})

    def export(self, h):
        data = self.q("/torrents/export", hash=h)
        return data if isinstance(data, bytes) and data.startswith(b"d") else None

    def add(self, content, filename, category):
        self.q.upload("/torrents/add", {"category": category or ""}, filename, content)

    def version(self):
        v = self.q("/app/version")
        if not v or b"Forbidden" in (v if isinstance(v, bytes) else b""):
            raise RuntimeError("wrong username or password")
        return "qBittorrent " + (v.decode() if isinstance(v, bytes) else str(v))


class Transmission:
    """Transmission's RPC API. Torrents come back in the same shape as qBittorrent's."""
    FIELDS = ["hashString", "name", "downloadDir", "sizeWhenDone", "percentDone", "uploadRatio", "secondsSeeding", "status", "error",
              "rateUpload", "rateDownload", "addedDate", "labels", "trackers", "torrentFile"]
    STATES = {1: "checkingUP", 2: "checkingUP", 3: "queuedDL", 4: "downloading", 5: "queuedUP"}

    def __init__(self, conf):
        url = conf["url"].rstrip("/")
        self.url = url if url.endswith("/rpc") else url + "/transmission/rpc"
        self.h = {"Content-Type": "application/json"}
        if conf.get("username") or conf.get("password"):
            tok = base64.b64encode(f"{conf.get('username', '')}:{conf.get('password', '')}".encode()).decode()
            self.h["Authorization"] = "Basic " + tok

    def rpc(self, method, **args):
        for attempt in (0, 1, 2):
            req = urllib.request.Request(self.url, json.dumps({"method": method, "arguments": args}).encode(), self.h)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 409 and attempt < 2:  # Transmission hands out a session id on the first call
                    self.h["X-Transmission-Session-Id"] = e.headers.get("X-Transmission-Session-Id", "")
                    continue
                if e.code == 401:
                    raise RuntimeError("wrong username or password")
                raise
            if out.get("result") != "success":
                raise RuntimeError(out.get("result") or "Transmission error")
            return out.get("arguments") or {}

    def torrents(self, hashes=None):
        args = {"fields": self.FIELDS, **({"ids": list(hashes)} if hashes else {})}
        out = []
        for t in self.rpc("torrent-get", **args)["torrents"]:
            done = t["percentDone"] >= 1
            state = "error" if t.get("error") else "pausedUP" if t["status"] == 0 and done else "pausedDL" if t["status"] == 0 \
                else ("uploading" if t["rateUpload"] else "stalledUP") if t["status"] == 6 else self.STATES.get(t["status"], "unknown")
            labels = t.get("labels") or []
            out.append({"hash": t["hashString"].lower(), "name": t["name"], "save_path": t["downloadDir"],
                        "content_path": os.path.join(t["downloadDir"], t["name"]), "size": t["sizeWhenDone"], "progress": t["percentDone"],
                        "ratio": max(0.0, t["uploadRatio"]), "seeding_time": t.get("secondsSeeding") or 0, "state": state,
                        "upspeed": t["rateUpload"], "dlspeed": t["rateDownload"], "added_on": t["addedDate"],
                        "category": labels[0] if labels else os.path.basename(t["downloadDir"].rstrip("/")),
                        "tracker": ((t.get("trackers") or [{}])[0]).get("announce", ""), "torrent_file": t.get("torrentFile")})
        return out

    def remove(self, hashes, with_data):
        self.rpc("torrent-remove", ids=list(hashes), **{"delete-local-data": bool(with_data)})

    def export(self, h):
        """Transmission keeps .torrent files in its own config folder; readable only if that folder is mounted here."""
        t = self.torrents([h])
        p = local(t[0].get("torrent_file") or "") if t else ""
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def add(self, content, filename, category):
        self.rpc("torrent-add", metainfo=base64.b64encode(content).decode(), **({"labels": [category]} if category else {}))

    def version(self):
        return "Transmission " + str(self.rpc("session-get", fields=["version"]).get("version", ""))


class Deluge:
    """Deluge's Web UI JSON API (with the Label plugin for categories). Torrents come back in qBittorrent's shape."""
    KEYS = ["name", "save_path", "download_location", "total_wanted", "progress", "ratio", "seeding_time", "state", "upload_payload_rate",
            "download_payload_rate", "time_added", "label", "trackers"]

    def __init__(self, conf):
        url = conf["url"].rstrip("/")
        self.url = url if url.endswith("/json") else url + "/json"
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.n = 0
        if not self.rpc("auth.login", conf.get("password", "")):
            raise RuntimeError("wrong password")
        if not self.rpc("web.connected"):
            hosts = self.rpc("web.get_hosts")
            if not hosts:
                raise RuntimeError("the Deluge web UI isn't connected to a Deluge daemon")
            self.rpc("web.connect", hosts[0][0])

    def rpc(self, method, *params):
        self.n += 1
        req = urllib.request.Request(self.url, json.dumps({"method": method, "params": list(params), "id": self.n}).encode(),
                                     {"Content-Type": "application/json"})
        with self.op.open(req, timeout=60) as r:
            out = json.loads(r.read())
        if out.get("error"):
            raise RuntimeError((out["error"] or {}).get("message") or "Deluge error")
        return out.get("result")

    def torrents(self, hashes=None):
        res = self.rpc("core.get_torrents_status", {"id": list(hashes)} if hashes else {}, self.KEYS) or {}
        out = []
        for h, t in res.items():
            done, st = t["progress"] >= 100, t.get("state") or ""
            state = {"Seeding": "uploading" if t["upload_payload_rate"] else "stalledUP", "Downloading": "downloading", "Error": "error",
                     "Checking": "checkingUP", "Moving": "moving", "Allocating": "allocating",
                     "Paused": "pausedUP" if done else "pausedDL", "Queued": "queuedUP" if done else "queuedDL"}.get(st, st.lower())
            save = t.get("download_location") or t.get("save_path") or ""
            out.append({"hash": h.lower(), "name": t["name"], "save_path": save, "content_path": os.path.join(save, t["name"]),
                        "size": t["total_wanted"], "progress": t["progress"] / 100, "ratio": max(0.0, t["ratio"]),
                        "seeding_time": t.get("seeding_time") or 0, "state": state, "upspeed": t["upload_payload_rate"],
                        "dlspeed": t["download_payload_rate"], "added_on": int(t["time_added"]), "category": t.get("label") or "",
                        "tracker": ((t.get("trackers") or [{}])[0]).get("url", "")})
        return out

    def remove(self, hashes, with_data):
        for h in hashes:
            self.rpc("core.remove_torrent", h, bool(with_data))

    def export(self, h):
        return None  # Deluge can't hand out a torrent's .torrent file

    def add(self, content, filename, category):
        tid = self.rpc("core.add_torrent_file", filename, base64.b64encode(content).decode(), {})
        if tid and category:
            try:
                if category not in (self.rpc("label.get_labels") or []):
                    self.rpc("label.add", category)
                self.rpc("label.set_torrent", tid, category)
            except RuntimeError:
                pass  # Label plugin not enabled

    def version(self):
        return "Deluge " + str(self.rpc("daemon.get_version"))


def make_client(kind, conf):
    return {"qbittorrent": QBitClient, "transmission": Transmission, "deluge": Deluge}[kind](conf)


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
            eps = arr_api(out["app"])("GET", "/episode", seriesId=out["seriesId"])
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


def jellyfin_series_ids(c, ev):
    """Provider ids (tvdb, tmdb, imdb) of the show an episode or season event belongs to, from Jellyfin."""
    if not ev.get("series_id"):
        return {}
    jf = c["jellyfin"]
    try:
        r = call("GET", jf["url"].rstrip("/") + "/Items?" + urllib.parse.urlencode({"ids": ev["series_id"], "fields": "ProviderIds"}),
                 {"X-Emby-Token": jf["api_key"]})
        if r.get("Items"):
            return {k.lower(): v for k, v in (r["Items"][0].get("ProviderIds") or {}).items()}
    except Exception as e:
        log(f"  Jellyfin series lookup failed ({e}); matching by title")
    return {}


def on_disk(p):
    """Does this file (as Sonarr/Radarr name it) still exist?"""
    return bool(p) and os.path.exists(local(p))


def on_movie(c, ev):
    """With more than one Radarr (say a 4K one), only the instances whose copy is really gone are cleaned up."""
    found = []
    for key, _, name, conf in arr_instances("radarr", c):
        radarr = Arr(conf)
        movies = radarr("GET", "/movie")
        m = next((m for m in movies if ev.get("tmdb_id") and m.get("tmdbId") == ev["tmdb_id"]), None) \
            or next((m for m in movies if ev.get("imdb_id") and m.get("imdbId") == ev["imdb_id"]), None)
        if not m:
            hits = [m for m in movies if m["title"].casefold() == (ev.get("name") or "").casefold()]
            m = hits[0] if len(hits) == 1 else None
        if m:
            found.append((key, name, radarr, m, m.get("hasFile") and on_disk((m.get("movieFile") or {}).get("path"))))
    if not found:
        return log("  no Radarr match")
    for key, name, radarr, m, kept in found:
        if kept:
            log(f"  '{m['title']}' still has its file in {name}, so that copy stays")
        else:
            movie_removed(c, key, name, radarr, m, seerr=not any(x[4] for x in found))


def movie_removed(c, key, name, radarr, m, seerr=True):
    hashes = {h["downloadId"] for h in radarr("GET", "/history/movie", movieId=m["id"]) if h.get("downloadId")}
    snap = {"kind": "movie", "app": key, "movie": {k: m.get(k) for k in MOVIE_KEYS}}
    queue_torrents(hashes, m["title"], "movie", snap, secrets.token_hex(6))
    bh = behavior()
    act(c, f"removed '{m['title']}' from {name}" + (" (with import exclusion)" if bh["import_exclusion"] else ""),
        lambda: radarr("DELETE", f"/movie/{m['id']}", deleteFiles="true", addImportExclusion=str(bh["import_exclusion"]).lower()))
    if bh["remove_from_seerr"] and seerr:
        seerr_remove(c, "movie", m.get("tmdbId"), m["title"])


def on_series(c, ev):
    found = []
    for key, _, name, conf in arr_instances("sonarr", c):
        sonarr = Arr(conf)
        s = find_series(sonarr, ev.get("tvdb_id"), ev.get("tmdb_id"), ev.get("imdb_id"), ev.get("name"))
        if s:
            found.append((key, name, sonarr, s, os.path.isdir(local(s["path"]))))
    if not found:
        return log("  no Sonarr match")
    for key, name, sonarr, s, kept in found:
        if kept:
            # Deleting a show in Jellyfin deletes its folder. If the folder is still there, Jellyfin only dropped a show
            # with no episodes left on disk (e.g. after its last episode was deleted), so keep it in Sonarr for new episodes.
            log(f"  '{s['title']}' folder still exists in {name}, so this was just an empty show being dropped; keeping it")
        else:
            series_removed(c, key, name, sonarr, s, seerr=not any(x[4] for x in found))


def series_removed(c, key, name, sonarr, s, seerr=True):
    hashes = {h["downloadId"] for h in sonarr("GET", "/history/series", seriesId=s["id"]) if h.get("downloadId")}
    snap = {"kind": "series", "app": key,
            "series": {k: s.get(k) for k in SERIES_KEYS},
            "seasons": [[x["seasonNumber"], x["monitored"]] for x in s["seasons"]],
            "monitored_eps": [[e["seasonNumber"], e["episodeNumber"]] for e in sonarr("GET", "/episode", seriesId=s["id"]) if e["monitored"]]}
    queue_torrents(hashes, s["title"], "series", snap, secrets.token_hex(6))
    bh = behavior()
    act(c, f"removed '{s['title']}' from {name}" + (" (with import list exclusion)" if bh["import_exclusion"] else ""),
        lambda: sonarr("DELETE", f"/series/{s['id']}", deleteFiles="true", addImportListExclusion=str(bh["import_exclusion"]).lower()))
    if bh["remove_from_seerr"] and seerr:
        seerr_remove(c, "tv", s.get("tmdbId"), s["title"])


def on_episodes(c, ev, season, numbers=None):
    """Whole season when numbers is None, otherwise the listed episode numbers."""
    ids, found = jellyfin_series_ids(c, ev), False
    for key, _, name, conf in arr_instances("sonarr", c):
        sonarr = Arr(conf)
        s = find_series(sonarr, ids.get("tvdb"), ids.get("tmdb"), ids.get("imdb"), ev.get("series_name"))
        if s:
            found = True
            episodes_deleted(c, key, name, sonarr, s, ev, season, numbers)
    if not found:
        log("  no Sonarr match for the series")


def episodes_deleted(c, key, name, sonarr, s, ev, season, numbers):
    dirs = os.listdir(local(s["path"])) if os.path.isdir(local(s["path"])) else []
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
    if eps and any(e.get("episodeFileId") for e in eps):
        fpath = {f["id"]: f["path"] for f in sonarr("GET", "/episodefile", seriesId=s["id"])}
        kept = [e for e in eps if e.get("episodeFileId") and on_disk(fpath.get(e["episodeFileId"]))]
        if kept:
            log(f"  '{s['title']}' {episodes_label(kept)} still {'have their files' if len(kept) > 1 else 'has its file'} in {name}, so "
                f"{'those copies stay' if len(kept) > 1 else 'that copy stays'}")
            eps = [e for e in eps if e not in kept]
            if not eps:
                return
    if not eps:
        return log(f"  no matching episodes in {name} for '{s['title']}'")
    label = f"'{s['title']}' S{season:02d}" + ("" if numbers is None else "".join(f"E{e['episodeNumber']:02d}" for e in eps))
    remove_episodes(c, key, name, sonarr, s, eps, label, season if numbers is None and not flat else None)


def remove_episodes(c, key, name, sonarr, s, eps, label, season=None):
    """Episodes deleted from the library: queue their torrents, unmonitor them (and the season, when a whole season with
    its folder went) and drop their file records. The show stays for new episodes."""
    ids = sorted(e["id"] for e in eps)
    hashes = {h["downloadId"] for h in sonarr("GET", "/history/series", seriesId=s["id"])
              if h.get("downloadId") and h.get("episodeId") in ids}
    season_was = season is not None and any(x["seasonNumber"] == season and x["monitored"] for x in s["seasons"])
    snap = {"kind": "episodes", "app": key, "series_id": s["id"], "title": s["title"],
            "remonitor": [e["id"] for e in eps if e["monitored"]], "seasons": [season] if season_was else []}
    queue_torrents(hashes, label, "season" if season is not None else "episode", snap, secrets.token_hex(6))
    bh = behavior()
    if bh["unmonitor"]:
        act(c, f"unmonitored {label} in {name}", lambda: sonarr("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": False}))
    if season is not None and bh["unmonitor"]:
        def unmonitor_season():
            full = sonarr("GET", f"/series/{s['id']}")
            for x in full["seasons"]:
                if x["seasonNumber"] == season:
                    x["monitored"] = False
            sonarr("PUT", f"/series/{s['id']}", full)
        act(c, f"unmonitored season {season} of '{s['title']}'", unmonitor_season)
    files = sorted({e["episodeFileId"] for e in eps if e.get("episodeFileId")})
    if files and bh["remove_episode_files"]:
        act(c, f"removed {len(files)} episode file record(s) of {label} from {name}",
            lambda: sonarr("DELETE", "/episodefile/bulk", {"episodeFileIds": files}))


def handle(ev):
    if ev.get("disk_deletes"):
        return disk_deletes(ev["disk_deletes"])
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


def tc(method, *args):
    """A torrent client call over a shared session, signing in again when the session expires."""
    global _qb
    for attempt in (0, 1):
        try:
            if _qb is None:
                _qb = make_client(client_kind(), cfg()[client_kind()])
            return getattr(_qb, method)(*args)
        except (urllib.error.HTTPError, RuntimeError) as e:
            _qb = None
            if attempt or (isinstance(e, urllib.error.HTTPError) and e.code not in (401, 403, 409)):
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


def torrents(hashes=None):
    """All torrents (or the given hashes) in qBittorrent's shape, with paths as this container sees them."""
    ts = tc("torrents", [hashes] if isinstance(hashes, str) else hashes)
    for t in ts:
        t["content_path"], t["save_path"] = local(t["content_path"]), local(t["save_path"])
    return ts


_roots = {"t": 0, "dirs": []}


def root_dirs():
    """Sonarr/Radarr root folders as this container sees them (refreshed every 5 minutes)."""
    if time.time() - _roots["t"] > 300:
        dirs = []
        for _, _, _, conf in arr_instances():
            try:
                dirs += [local(r["path"].rstrip("/")) for r in Arr(conf)("GET", "/rootfolder")]
            except Exception:
                pass
        _roots.update(t=time.time(), dirs=sorted(set(dirs)))
    return _roots["dirs"]


def library_path(p):
    """History keeps the path at import time. If a library root folder moved since (e.g. /media/Movies -> /data/Media/Movies),
    find the file under the current root folder of the same name."""
    p = local(p)
    if not p or os.path.exists(p):
        return p
    for root in root_dirs():
        name = "/" + os.path.basename(root) + "/"
        i = p.find(name)
        if i >= 0:
            return root + p[i + len(name) - 1:]
    return p


def import_map(max_age=300):
    """torrent hash -> library imports recorded by Sonarr/Radarr ({app, path, movieId, seriesId, episodeId})."""
    if time.time() - _imports["t"] < max_age:
        return _imports["map"]
    m, owner = {}, {}
    for app, _, name, conf in arr_instances():
        a, page = Arr(conf), 1
        try:
            while True:  # newest first, so the first import seen for a path is the one that's there now
                r = a("GET", "/history", page=page, pageSize=1000, eventType=3, sortKey="date", sortDirection="descending")  # 3 = downloadFolderImported
                for h in r["records"]:
                    if h.get("downloadId"):
                        path = library_path((h.get("data") or {}).get("importedPath"))
                        m.setdefault(h["downloadId"].lower(), []).append(
                            {"app": app, "path": path, "movieId": h.get("movieId"), "seriesId": h.get("seriesId"), "episodeId": h.get("episodeId")})
                        if path:
                            owner.setdefault(path, h["downloadId"].lower())
                if page * 1000 >= r["totalRecords"]:
                    break
                page += 1
        except Exception as e:
            if app in ARR_KINDS:
                raise
            print(f"history of {name}:", e, flush=True)  # an extra instance being down shouldn't stop everything
    _imports.update(t=time.time(), map=m, owner=owner)
    return m


def import_owner(max_age=300):
    """library path -> hash of the torrent it was last imported from."""
    import_map(max_age)
    return _imports.get("owner", {})


def file_key(p):
    """A file's identity on disk. Hardlinks to the same data share it, whatever their paths."""
    try:
        st = os.stat(p)
        return st.st_dev, st.st_ino
    except OSError:
        return None


def nlink(p):
    try:
        return os.stat(p).st_nlink
    except OSError:
        return 0


_libidx = {"t": 0, "map": {}, "ok": False}


def library_index(max_age=300):
    """(device, inode) -> path, for every video file in the Sonarr/Radarr root folders."""
    if time.time() - _libidx["t"] > max_age:
        m, dirs = {}, root_dirs()
        for root in dirs:
            for d, _, fs in os.walk(root):
                for p in videos_in(os.path.join(d, f) for f in fs):
                    if k := file_key(p):
                        m[k] = p
        _libidx.update(t=time.time(), map=m, ok=any(os.path.isdir(r) for r in dirs))
    return _libidx["map"]


def torrent_keys(t):
    return {k for p in videos_in(files_under(t["content_path"])) if (k := file_key(p))}


def own_imports(h, max_age=60):
    """Library paths Sonarr/Radarr imported from torrent h that still exist and weren't replaced by a later import
    (an upgrade from another download)."""
    owner = import_owner(max_age)
    return {i["path"] for i in import_map(max_age).get(h, []) if i["path"] and owner.get(i["path"]) == h and os.path.exists(i["path"])}


def in_library(t, max_age=0):
    """How many library files are this torrent's: library files that are the same file on disk as one of the torrent's
    (hardlinks), or, for RAR releases (whose extracted copy isn't linked to the torrent), the paths Sonarr/Radarr last
    imported from it. A cross-seed of the same release counts too, since it shares the files."""
    hit = _lib_cache.get(t["hash"])
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    idx, linked = library_index(), 0
    for p in videos_in(files_under(t["content_path"])):
        try:
            st = os.stat(p)
        except OSError:
            continue
        k = (st.st_dev, st.st_ino)
        if st.st_nlink > 1 and k not in idx and time.time() - _libidx["t"] > 60:
            idx = library_index(60)  # maybe imported since the last scan
        if not _libidx["ok"]:
            linked += st.st_nlink > 1  # library folders unknown (Sonarr/Radarr unreachable): any second link counts
        elif k in idx and file_key(idx[k]) == k:
            linked += 1
    n = max(linked, len(own_imports(t["hash"], max(max_age, 60))))
    _lib_cache[t["hash"]] = (time.time(), n)
    return n


_groups = {"t": 0, "map": {}}


def cross_seed_groups(ts, max_age=60):
    """hash -> every torrent sharing at least one video file with it: cross-seeds, the same release from other trackers,
    seeding from the same data or hardlinks to it. Torrents that share nothing aren't listed."""
    if time.time() - _groups["t"] < max_age:
        return _groups["map"]
    parent, first = {}, {}

    def root(x):
        while parent.setdefault(x, x) != x:
            parent[x] = x = parent[parent[x]]
        return x
    for t in ts:
        for k in torrent_keys(t):
            if k in first:
                parent[root(t["hash"])] = root(first[k])
            else:
                first[k] = t["hash"]
    sets = {}
    for h in list(parent):
        sets.setdefault(root(h), set()).add(h)
    m = {h: g for g in sets.values() if len(g) > 1 for h in g}
    _groups.update(t=time.time(), map=m)
    return m


def group_original(g, ts):
    """The torrent of a cross-seed group that Sonarr/Radarr imported (else the oldest). The others are its cross-seeds."""
    imap = import_map()
    added = lambda h: (ts.get(h) or {}).get("added_on", 0)
    g = [h for h in g if h in ts] or g  # ts loses torrents as they're deleted
    tracked = [h for h in g if h in imap]
    return min(tracked or g, key=added)


def remove_torrents(hashes, ts):
    """Delete torrents from qBittorrent with their data, except when another torrent that stays still seeds from the same
    files (a cross-seed sharing the folder): then the torrent goes but the data stays. Removed torrents are taken out of
    ts. Returns ({hash: bytes freed}, {hashes whose data was kept})."""
    hashes = {h for h in hashes if h in ts}
    keep_paths, keep_keys = set(), set()
    for x, t in ts.items():
        if x not in hashes:
            for p in files_under(t["content_path"]):
                keep_paths.add(os.path.realpath(p))
                if k := file_key(p):
                    keep_keys.add(k)
    freed, with_data, without, seen = {}, [], [], set()
    for h in hashes:
        t, n = ts[h], 0
        paths = files_under(t["content_path"])
        (without if any(os.path.realpath(p) in keep_paths for p in paths) else with_data).append(h)
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            k = (st.st_dev, st.st_ino)
            if k not in keep_keys and k not in seen:
                seen.add(k)
                n += st.st_size
        freed[h] = n if paths else t["size"]
    if with_data:
        tc("remove", with_data, True)
    if without:
        tc("remove", without, False)
    for h in hashes:
        ts.pop(h, None)
    return freed, set(without)


_indexers = {"t": 0, "by_hash": {}, "prowlarr": []}
_tnames = {"t": 0, "map": {}}


def indexer_info(max_age=600):
    """Which indexer (tracker site) each torrent was grabbed from, from Sonarr/Radarr's grab history, plus the torrent
    indexers set up in Prowlarr."""
    if time.time() - _indexers["t"] < max_age:
        return _indexers
    c, by_hash, prow = cfg(), {}, []
    for _, _, _, conf in arr_instances(c=c):
        try:
            a, page = Arr(conf), 1
            while True:
                r = a("GET", "/history", page=page, pageSize=1000, eventType=1)  # 1 = grabbed
                for h in r["records"]:
                    name = re.sub(r"\s*\(Prowlarr\)$", "", (h.get("data") or {}).get("indexer") or "")
                    if h.get("downloadId") and name:
                        by_hash.setdefault(h["downloadId"].lower(), name)
                if page * 1000 >= r["totalRecords"]:
                    break
                page += 1
        except Exception:
            pass
    if (c.get("prowlarr") or {}).get("url"):
        try:
            for i in Arr(c["prowlarr"])("GET", "/indexer"):
                if i.get("protocol") == "torrent":
                    urls = [f.get("value") for f in i.get("fields", []) if f.get("name") == "baseUrl"] + (i.get("indexerUrls") or [])
                    hosts = [urllib.parse.urlparse(u).hostname for u in urls if u]
                    prow.append({"name": i["name"], "hosts": sorted({h[4:] if h.startswith("www.") else h for h in hosts if h})})
        except Exception as e:
            print("prowlarr:", e, flush=True)
    _indexers.update(t=time.time(), by_hash=by_hash, prowlarr=prow)
    return _indexers


def site_of(host):
    """tracker.example.org -> example.org (good enough to match an announce host to a Prowlarr site)."""
    parts = (host or "").split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and len(parts[-1]) == 2 and len(parts[-2]) <= 3 else parts[-2:])


def tracker_names(ts=None, max_age=60):
    """hash -> tracker name (e.g. TorrentDay). From the grab history; torrents that weren't grabbed by Sonarr/Radarr get
    the name other torrents with the same announce host have, or a Prowlarr indexer on the same site."""
    if ts is None and time.time() - _tnames["t"] < max_age:
        return _tnames["map"]
    if ts is None:
        ts = torrents()
    info, by_host, out = indexer_info(), {}, {}
    for t in ts:
        if n := info["by_hash"].get(t["hash"]):
            by_host.setdefault(tracker_host(t), []).append(n)
    by_host = {h: max(set(ns), key=ns.count) for h, ns in by_host.items()}
    by_site = {site_of(h): i["name"] for i in info["prowlarr"] for h in i["hosts"]}
    for t in ts:
        host = tracker_host(t)
        n = info["by_hash"].get(t["hash"]) or by_host.get(host) or by_site.get(site_of(host))
        if n:
            out[t["hash"]] = n
    _tnames.update(t=time.time(), map=out)
    return out


def overrides(con):
    return {h: {"mode": m, "min_seed_days": d, "min_ratio": r}
            for h, m, d, r in con.execute("select hash, mode, days, ratio from overrides")}


_titles = {"t": 0, "series": {}, "movies": {}}


def titles():
    """Sonarr/Radarr id -> title, refreshed every 5 minutes (for per-show rules and labels)."""
    if time.time() - _titles["t"] > 300:
        series, movies = {}, {}
        for key, kind, _, conf in arr_instances():
            try:
                if kind == "sonarr":
                    series.update({(key, x["id"]): x["title"] for x in Arr(conf)("GET", "/series")})
                else:
                    movies.update({(key, x["id"]): x["title"] for x in Arr(conf)("GET", "/movie")})
            except Exception:
                pass
        _titles.update(t=time.time(), series=series or _titles["series"], movies=movies or _titles["movies"])
    return _titles


def title_of(h, pend_name=None):
    imps = import_map().get(h, [])
    tl = titles()
    for i in imps:
        if (i["app"], i.get("seriesId")) in tl["series"]:
            return tl["series"][(i["app"], i["seriesId"])]
        if (i["app"], i.get("movieId")) in tl["movies"]:
            return tl["movies"][(i["app"], i["movieId"])]
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
    tname = (tracker_names().get(t["hash"]) or "").casefold() if any(r.get("type") == "tracker" for r in extra) else ""
    for kind, test in (("title", lambda m: title and m.casefold() == title.casefold()),
                       ("tracker", lambda m: (host and (host == m or host.endswith("." + m))) or (tname and tname == m.casefold())),
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
    h = t["hash"]
    with db() as con:
        row = con.execute("select undo, content from pending where hash=?", (h,)).fetchone()
    snap = json.loads(row[0]) if row and row[0] else {}
    content = json.loads(row[1]) if row and row[1] else None
    if not content:
        try:
            content = torrent_content(h)
        except Exception:
            content = {"episodes": []}
    app = snap.get("app") or content.get("app")
    out = {"hash": h, "name": t["name"], "category": t.get("category"), "episodes": content.get("episodes") or [], "movie": None,
           "series": None, "app": app}
    try:
        if snap.get("kind") == "movie":
            out["movie"] = snap["movie"]
        elif content.get("movieId"):
            m = arr_api(app or "radarr")("GET", f"/movie/{content['movieId']}")
            out["movie"] = {k: m.get(k) for k in MOVIE_KEYS}
        if snap.get("kind") == "series":
            out["series"] = {**snap["series"], "seasons": snap["seasons"]}
        else:
            sid = snap.get("series_id") or content.get("seriesId")
            if sid:
                sr = arr_api(app or "sonarr")("GET", f"/series/{sid}")
                out["series"] = {**{k: sr.get(k) for k in SERIES_KEYS}, "seasons": [[x["seasonNumber"], x["monitored"]] for x in sr["seasons"]]}
    except Exception as e:
        print("capture_restore:", e, flush=True)
    try:
        data = tc("export", h)
        if data:
            os.makedirs(TORRENT_DIR, exist_ok=True)
            with open(os.path.join(TORRENT_DIR, h + ".torrent"), "wb") as f:
                f.write(data)
    except Exception as e:
        print("torrent export:", e, flush=True)
    return out


def record_cleanup(t, name, reason, restore=None, size=None):
    with db() as con:
        con.execute("insert into cleaned (ts, name, size, title, category, reason, hash, restore) values (?,?,?,?,?,?,?,?)",
                    (time.time(), t["name"], t["size"] if size is None else size, title_of(t["hash"], name) or name, t.get("category"),
                     reason, t["hash"], json.dumps(restore) if restore else None))


def cross_seed_of(h, ts, grp):
    """The original torrent if h is a cross-seed of another torrent, else None."""
    g = grp.get(h)
    orig = g and group_original(g, ts)
    return orig if orig and orig != h and orig in ts else None


def cleanup_torrents(batch, ts, grp, name, reason):
    """Delete these torrents (one, or an original with its cross-seeds), keeping History and Redownload info.
    Returns [(torrent, bytes freed, data kept)]."""
    gone = [(ts[h], cross_seed_of(h, ts, grp)) for h in batch if h in ts]
    restores = {t["hash"]: ({"cross_seed_of": ts[o]["name"]} if o else capture_restore(t)) for t, o in gone}
    freed, kept = remove_torrents([t["hash"] for t, _ in gone], ts)
    for t, o in gone:
        record_cleanup(t, name, reason, restores[t["hash"]], freed.get(t["hash"]))
    return [(t, freed.get(t["hash"], 0), t["hash"] in kept) for t, _ in gone]


def check(c, t, name, r, added=None, ctx=None):
    """Returns a status string, or None once the torrent is gone. r is the rule that applies. ctx carries all torrents,
    the cross-seed groups, what's queued, and collects torrents that went along with this one."""
    if not t:
        log(f"torrent for {name} is gone from {client_name()}; dropping it from the queue")
        return None
    if t["progress"] < 1:
        return "still downloading"
    ctx = ctx or {"ts": {t["hash"]: t}, "groups": {}, "pending": set(), "removed": set()}
    ts, grp, together = ctx["ts"], ctx["groups"], behavior()["cross_seed_with_original"]
    orig = cross_seed_of(t["hash"], ts, grp)
    if orig and together and orig in ctx["pending"]:
        return f"cross-seed: deleted together with '{ts[orig]['name']}'"
    progress, met = evaluate(r, t)
    left = in_library(t)
    if left:
        return f"waiting: {left} file(s) still in library; {progress}"
    if not met:
        return f"seeding: {progress}"
    wait = (added or 0) + grace_seconds() - time.time()
    if added and wait > 0:
        return f"deletion delay: deletes in {duration(wait)} (Undo still possible); {progress}"
    batch = [t["hash"]]
    if together and not orig:
        batch += sorted(x for x in grp.get(t["hash"], ()) if x != t["hash"] and x in ctx["pending"] and x in ts)
    if c.get("dry_run"):
        log(f"[dry run] would have deleted torrent '{t['name']}' and its data ({progress})"
            + (f", with {len(batch) - 1} cross-seed{'s' * (len(batch) > 2)}" if len(batch) > 1 else ""))
        return f"[dry run] ready to delete; {progress}"
    for x, freed, kept in cleanup_torrents(batch, ts, grp, name, "seeding rule met"):
        log(f"deleted torrent '{x['name']}'" + (" (its data stays: another torrent still seeds from it)" if kept else " and its data")
            + (f" ({progress})" if x is t else " (cross-seed, together with the original)"))
        notify("Torrent cleaned up", f"{name}\n{x['name']} · {human(freed)} freed" + (f" ({progress})" if x is t else " (cross-seed)"), "cleanup")
        if x is not t:
            ctx["removed"].add(x["hash"])
    return None


def queue_cross_seeds(rows, ts, grp):
    """Cross-seeds of torrents waiting for cleanup are cleaned up too (each by its own seeding rule, unless Settings say
    they go together with the original)."""
    pend, new = {h for h, _, _ in rows}, []
    for h, name, _ in rows:
        for x in sorted(grp.get(h, ())):
            if x not in pend and x in ts:
                pend.add(x)
                new.append((x, name))
    if new:
        with db() as con:
            for x, name in new:
                con.execute("insert or ignore into pending (hash, name, source, added_at, status) values (?,?,?,?,?)",
                            (x, name, "cross-seed", time.time(), "queued"))
        for x, name in new:
            log(f"  cross-seed '{ts[x]['name']}'" + (f" ({tracker_host(ts[x])})" if tracker_host(ts[x]) else "")
                + f" of {name} queued for cleanup too")
    return bool(new)


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
        grp = cross_seed_groups(ts.values(), 0)
        if queue_cross_seeds(rows, ts, grp):
            with db() as con:
                rows = con.execute("select hash, name, added_at from pending order by added_at").fetchall()
        ctx = {"ts": ts, "groups": grp, "pending": {h for h, _, _ in rows}, "removed": set()}
        for h, name, added in rows:
            if h in ctx["removed"]:
                with db() as con:
                    con.execute("delete from pending where hash=?", (h,))
                continue
            t = ts.get(h)
            try:
                status = check(c, t, name, rule_for(t, ov, c, title_of(h, name)) if t else None, added, ctx)
            except Exception as e:
                status = f"error: {e}"
            with db() as con:
                if status is None:
                    con.execute("delete from pending where hash=?", (h,))
                    ctx["pending"].discard(h)
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
        [(_, freed, _)] = cleanup_torrents([h], ts, cross_seed_groups(ts.values()), name, "low disk space")
        with db() as con:
            con.execute("delete from pending where hash=?", (h,))
        notify("Torrent cleaned up early", f"Low disk space: {t['name']} · {human(freed)} freed", "cleanup")
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


def media_steps(c, paths, movies=(), series=()):
    """Steps that remove media everywhere: the library files at paths from Jellyfin (which deletes them), movies from
    Radarr (and Seerr), and episodes in Sonarr (unmonitored, files removed; the series itself stays so new episodes keep
    coming). With Plex, Sonarr/Radarr delete the files and Plex rescans. movies = [(instance key, movie)],
    series = [(instance key, series, [episodes])]."""
    steps, bh, plex_mode = [], behavior(), media_kind(c) == "plex"
    targets = {tail(p) for p in paths} if not plex_mode else set()
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
    for key, m in movies:
        excl, radarr = bh["import_exclusion"], arr_api(key)
        steps.append((f"Remove '{m['title']}' from {arr_name(key)}, with its files" + (" (and add an import exclusion)" if excl else ""),
                       lambda m=m, radarr=radarr: radarr("DELETE", f"/movie/{m['id']}", deleteFiles="true", addImportExclusion=str(excl).lower())))
        if bh["remove_from_seerr"]:
            steps.append(seerr_step(c, "movie", m.get("tmdbId"), m["title"]))
    for key, s, mine in series:
        label, sonarr = f"'{s['title']}' {episodes_label(mine)}", arr_api(key)
        ids = sorted(e["id"] for e in mine)
        if bh["unmonitor"]:
            steps.append((f"Unmonitor {label} in {arr_name(key)}",
                          lambda ids=ids, sonarr=sonarr: sonarr("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": False})))
        files = sorted({e["episodeFileId"] for e in mine if e.get("episodeFileId")})
        if files:
            steps.append((f"{'Delete' if plex_mode else 'Remove'} {label} files {'with' if plex_mode else 'from'} {arr_name(key)}",
                          lambda files=files, sonarr=sonarr: sonarr("DELETE", "/episodefile/bulk", {"episodeFileIds": files})))
    if plex_mode and paths:
        steps.append(("Rescan Plex so it drops the deleted files", lambda: media_rescan(paths, deleted=True)))
    return [x for x in steps if x]


def build_plan(c, h):
    """Everything 'Delete now' would remove for torrent h: (torrent, [(description, action)], info). An action of None is
    only a note. Only movies and episodes whose current file came from this torrent (or its cross-seeds) are touched:
    if Sonarr/Radarr has since upgraded one to a file from another download, it's left alone."""
    ts = {x["hash"]: x for x in torrents()}
    t = ts.get(h)
    group = sorted(cross_seed_groups(ts.values(), 0).get(h, {h}) if t else {h}, key=lambda x: x != h)
    imap = import_map(0)
    imps = [i for x in group for i in imap.get(x, [])]
    keys = set().union(*[torrent_keys(ts[x]) for x in group if x in ts])
    idx = library_index(0)
    owner = import_owner(0)
    paths = {p for k, p in idx.items() if k in keys} | {i["path"] for i in imps if i["path"] and owner.get(i["path"]) in group and os.path.exists(i["path"])}
    verify = _libidx["ok"]  # library folders readable, so file ownership can be checked

    def from_elsewhere(p):
        """True if this library file exists and isn't one of this torrent's."""
        return verify and bool(p) and os.path.exists(p) and file_key(p) not in keys and p not in paths
    notes, movies, series = [], [], []
    for key, mid in sorted({(i["app"], i["movieId"]) for i in imps if kind_of(i["app"]) == "radarr" and i["movieId"]}):
        try:
            m = arr_api(key)("GET", f"/movie/{mid}")
        except (urllib.error.HTTPError, RuntimeError):
            continue
        if m.get("hasFile") and from_elsewhere(local((m.get("movieFile") or {}).get("path"))):
            notes.append((f"Keep '{m['title']}' in {arr_name(key)} and {media_name()}: its current file came from a different download", None))
        else:
            movies.append((key, m))
    by_series = {}
    for i in imps:
        if kind_of(i["app"]) == "sonarr" and i["episodeId"]:
            by_series.setdefault((i["app"], i["seriesId"]), set()).add(i["episodeId"])
    for (key, sid), epids in by_series.items():
        try:
            sonarr = arr_api(key)
            s = sonarr("GET", f"/series/{sid}")
        except (urllib.error.HTTPError, RuntimeError):
            continue
        eps = [e for e in sonarr("GET", "/episode", seriesId=sid) if e["id"] in epids]
        fpath = {f["id"]: local(f["path"]) for f in sonarr("GET", "/episodefile", seriesId=sid)} if any(e.get("episodeFileId") for e in eps) else {}
        other = [e for e in eps if e.get("episodeFileId") and from_elsewhere(fpath.get(e["episodeFileId"]))]
        if other:
            notes.append((f"Keep '{s['title']}' {episodes_label(other)} in {arr_name(key)} and {media_name()}: "
                          f"{'their current files' if len(other) > 1 else 'its current file'} came from a different download", None))
        mine = [e for e in eps if e not in other]
        if mine:
            series.append((key, s, mine))
    steps = media_steps(c, paths, movies, series) + notes
    freed = {}
    for x in group:
        if x in ts:
            what = "cross-seed torrent" if x != h else "torrent"
            steps.append((f"Delete {what} '{ts[x]['name']}'" + (f" ({tracker_host(ts[x])})" if x != h and tracker_host(ts[x]) else "")
                          + f" and its downloaded data from {client_name()}", lambda x=x: freed.update(remove_torrents([x], ts)[0])))
    if t and not imps:  # not managed by Sonarr/Radarr (e.g. Courses, which the media server reads straight from the download folder)
        steps.append((f"Rescan the {media_name()} library so it drops the deleted files", media_refresh_all))
    return t, steps, {"group": [x for x in group if x in ts], "ts": dict(ts), "freed": freed, "paths": paths}


def delete_now(h):
    c = cfg()
    try:
        t, steps, info = build_plan(c, h)
    except Exception as e:
        return log(f"Delete now failed while planning: {e!r}")
    log(f"Delete now: {t['name'] if t else h}")
    all_ts, group = info["ts"], info["group"]
    restores = {x: ({"cross_seed_of": t["name"]} if x != h else capture_restore(all_ts[x])) for x in group} if not c.get("dry_run") else {}
    if not c.get("dry_run"):
        watch_suppress(info["paths"])
    for desc, fn in steps:
        if fn is None:
            log(f"  {desc}")
        elif c.get("dry_run"):
            log(f"  [dry run] would: {desc}")
        else:
            try:
                fn()
                log(f"  done: {desc}")
            except Exception as e:
                log(f"  FAILED: {desc}: {e}")
    if not c.get("dry_run"):
        with db() as con:
            for x in group or [h]:
                con.execute("delete from pending where hash=?", (x,))
                con.execute("delete from overrides where hash=?", (x,))
        for x in group:
            if x in info["freed"]:
                record_cleanup(all_ts[x], t["name"], "Delete now", restores.get(x), info["freed"][x])
        _imports["t"] = _libidx["t"] = _groups["t"] = 0
        if t:
            notify("Deleted now", f"{t['name']} · {human(sum(info['freed'].values()))} freed", "cleanup")


# ---------- media with no torrent ----------

_orphans = {"t": 0, "items": []}


def torrent_file_check():
    """A function telling whether a library file still belongs to a torrent in the torrent client: it is one of a torrent's
    files (hardlink), or it was last imported from a torrent that's still there (RAR releases). Files that can't be
    read count as belonging, so nothing is offered for deletion by mistake."""
    ts = torrents()
    hashes = {t["hash"] for t in ts}
    keys = set().union(*[torrent_keys(t) for t in ts])
    owner = import_owner(60)

    def has_torrent(p):
        k = file_key(p)
        return k is None or k in keys or owner.get(p) in hashes
    return has_torrent


def media_without_torrent(max_age=300):
    """Movies and seasons in Sonarr/Radarr whose files don't come from any torrent in the torrent client anymore, biggest first."""
    if time.time() - _orphans["t"] < max_age:
        return _orphans
    has_torrent, items = torrent_file_check(), []
    for key, kind, name, conf in arr_instances("radarr"):
        for m in Arr(conf)("GET", "/movie"):
            f = m.get("movieFile")
            if m.get("hasFile") and f and not has_torrent(p := local(f["path"])):
                items.append({"id": f"{key}:{m['id']}", "app": key, "kind": kind, "instance": name, "title": m["title"], "year": m.get("year"),
                              "what": "Movie", "size": f.get("size") or 0, "files": 1, "linked": nlink(p) > 1, "path": p})
    for key, kind, name, conf in arr_instances("sonarr"):
        sonarr = Arr(conf)
        for s in sonarr("GET", "/series"):
            if not (s.get("statistics") or {}).get("episodeFileCount"):
                continue
            lone = {f["id"]: f for f in sonarr("GET", "/episodefile", seriesId=s["id"]) if not has_torrent(local(f["path"]))}
            if not lone:
                continue
            eps = [e for e in sonarr("GET", "/episode", seriesId=s["id"]) if e.get("episodeFileId") in lone]
            for sn in sorted({f["seasonNumber"] for f in lone.values()}):
                fs = [f for f in lone.values() if f["seasonNumber"] == sn]
                items.append({"id": f"{key}:{s['id']}:{sn}", "app": key, "kind": kind, "instance": name, "title": s["title"], "year": s.get("year"),
                          "what": episodes_label([e for e in eps if e["seasonNumber"] == sn]) or f"Season {sn}",
                          "size": sum(f.get("size") or 0 for f in fs), "files": len(fs),
                          "linked": any(nlink(local(f["path"])) > 1 for f in fs), "path": os.path.dirname(local(fs[0]["path"]))})
    items.sort(key=lambda x: -x["size"])
    _orphans.update(t=time.time(), items=items)
    return _orphans


def orphan_plan(c, ids):
    """Delete steps for items from media_without_torrent (checked again, in case a torrent showed up meanwhile).
    Returns (steps, [(label, size, restore info)], library paths)."""
    has_torrent = torrent_file_check()
    paths, movies, series, done = set(), [], [], []
    for i in ids:
        app, *rest = str(i).split(":")
        if kind_of(app) == "radarr" and rest:
            m = arr_api(app)("GET", f"/movie/{int(rest[0])}")
            f = m.get("movieFile")
            if m.get("hasFile") and f and not has_torrent(p := local(f["path"])):
                paths.add(p)
                movies.append((app, m))
                done.append((m["title"], f.get("size") or 0 if nlink(p) <= 1 else 0,
                             {"app": app, "movie": {k: m.get(k) for k in MOVIE_KEYS}, "episodes": []}))
        elif kind_of(app) == "sonarr" and len(rest) == 2:
            sid, sn, sonarr = int(rest[0]), int(rest[1]), arr_api(app)
            s = sonarr("GET", f"/series/{sid}")
            lone = {f["id"]: f for f in sonarr("GET", "/episodefile", seriesId=sid)
                    if f["seasonNumber"] == sn and not has_torrent(local(f["path"]))}
            eps = [e for e in sonarr("GET", "/episode", seriesId=sid) if e.get("episodeFileId") in lone]
            if eps:
                paths |= {local(f["path"]) for f in lone.values()}
                series.append((app, s, eps))
                done.append((f"{s['title']} {episodes_label(eps)}", sum(f.get("size") or 0 for f in lone.values() if nlink(local(f["path"])) <= 1),
                             {"app": app, "series": {**{k: s.get(k) for k in SERIES_KEYS}, "seasons": [[x["seasonNumber"], x["monitored"]] for x in s["seasons"]]},
                              "episodes": sorted([e["seasonNumber"], e["episodeNumber"]] for e in eps)}))
    return media_steps(c, paths, movies, series), done, paths


def delete_orphans(ids):
    c = cfg()
    try:
        steps, done, paths = orphan_plan(c, ids)
    except Exception as e:
        return log(f"Deleting media with no torrent failed while planning: {e!r}")
    if not c.get("dry_run"):
        watch_suppress(paths)
    log(f"Delete media with no torrent: {', '.join(x[0] for x in done) or 'nothing left to delete'}")
    for desc, fn in steps:
        if c.get("dry_run"):
            log(f"  [dry run] would: {desc}")
            continue
        try:
            fn()
            log(f"  done: {desc}")
        except Exception as e:
            log(f"  FAILED: {desc}: {e}")
    if not c.get("dry_run") and done:
        with db() as con:
            for label, size, restore in done:
                con.execute("insert into cleaned (ts, name, size, title, category, reason, hash, restore) values (?,?,?,?,?,?,?,?)",
                            (time.time(), label, size, label, None, "no torrent", None,
                             json.dumps({"hash": None, "name": label, "category": None, "movie": None, "series": None, **restore})))
        _orphans["t"] = _imports["t"] = _libidx["t"] = 0
        notify("Deleted media with no torrent", f"{', '.join(x[0] for x in done)} · {human(sum(x[1] for x in done))} freed", "cleanup")


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


def snap_app(snap):
    """The Sonarr/Radarr instance an Undo snapshot belongs to."""
    return snap.get("app") or ("radarr" if snap["kind"] == "movie" else "sonarr")


def undo_steps(t, rows):
    snap = rows[0][2]
    app = arr_name(snap_app(snap))
    if snap["kind"] == "movie":
        first = f"Re-add '{snap['movie']['title']}' to {app} with its previous settings (if it was removed)"
    elif snap["kind"] == "series":
        first = f"Re-add '{snap['series']['title']}' to {app} with its previous settings and monitoring"
    else:
        first = f"Monitor {rows[0][1]} again in {app}"
    n = len(rows)
    middle = (f"Have {app} re-import the file{'s' if n > 1 else ''} from {'these torrents' if n > 1 else 'this torrent'}'s download, "
              f"so it's back in your library and {media_name()}")
    if restorable(t) == "rar":
        middle = (f"Unpack it again from the torrent's RAR files (takes a minute or two), then have {app} import it, "
                  f"so it's back in your library and {media_name()}")
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
    media_rescan([i["path"] for i in import_map(0).get(t["hash"], []) if i["path"] and os.path.exists(i["path"])])
    return f"{n} file{'s' * (n != 1)} back in the library"


def media_rescan(paths, deleted=False):
    """Tell the media server about restored (or, with Plex, deleted) library files."""
    if media_kind() == "plex":
        plex_rescan(paths, deleted)
    elif not deleted:
        jellyfin_rescan(paths)


def media_refresh_all():
    if media_kind() == "plex":
        for s in plex("GET", "/library/sections")["MediaContainer"].get("Directory", []):
            plex("GET", f"/library/sections/{s['key']}/refresh")
    else:
        jellyfin("POST", "/Library/Refresh")


def plex(method, endpoint, **q):
    p = cfg()["plex"]
    return call(method, p["url"].rstrip("/") + endpoint + ("?" + urllib.parse.urlencode(q) if q else ""),
                {"X-Plex-Token": p.get("api_key", ""), "Accept": "application/json"})


def plex_rescan(paths, deleted=False):
    """Partial Plex scans of the folders restored files are in. Plex may see the library at another path, so each file is
    mapped onto Plex's own library folders by folder name. After deletes, the whole library section is scanned instead:
    Plex merges a movie's versions from different folders (1080p and 4K) into one item, and a scan of only one folder
    would drop the other version too until its folder is scanned."""
    try:
        sections = plex("GET", "/library/sections")["MediaContainer"].get("Directory", [])
    except Exception as e:
        return print("plex rescan:", e, flush=True)
    todo = set()
    for p in paths:
        for s in sections:
            for loc in s.get("Location", []):
                name = "/" + os.path.basename(loc["path"].rstrip("/")) + "/"
                i = p.find(name)
                if i >= 0:
                    todo.add((s["key"], None if deleted else os.path.dirname(loc["path"].rstrip("/") + p[i + len(name) - 1:])))
    for key, folder in todo:
        try:
            plex("GET", f"/library/sections/{key}/refresh", **({"path": folder} if folder else {}))
        except Exception as e:
            print("plex rescan:", e, flush=True)


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
    arr = arr_api(snap_app(snap))
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
    try:
        grp = cross_seed_groups(torrents(), 0)
    except Exception:
        grp = {}
    sibs = {x for h0, _, _ in rows for x in grp.get(h0, ())} - {x for x, _, _ in rows}
    with db() as con:  # off the cleanup list first, so a sweep can't delete them while they're being restored
        con.executemany("delete from pending where hash=?", [(x,) for x, _, _ in rows])
        con.executemany("delete from pending where hash=? and source='cross-seed'", [(x,) for x in sibs])
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
    by_hash = {t["hash"]: t for t in raw}
    grp = cross_seed_groups(raw)
    tnames = tracker_names(raw)
    together = behavior()["cross_seed_with_original"]
    for t in raw:
        h = t["hash"]
        orig = cross_seed_of(h, by_hash, grp)
        name = pend[h][0] if h in pend else None
        title = title_of(h, name) or (title_of(orig) if orig else None)
        r = rule_for(t, ov, c, title)
        progress, met = evaluate(r, t)
        lib = in_library(t, max_age=60)
        managed = h in imap or any(x in imap for x in grp.get(h, ()))
        out.append({"hash": h, "name": t["name"], "title": title, "category": t["category"], "tracker": tracker_host(t),
                    "tracker_name": tnames.get(h), "cross_seed_of": by_hash[orig]["name"] if orig else None,
                    "cross_seeds": len(grp.get(h, ())) - 1 if h in grp and not orig else 0,
                    "with_original": bool(orig and together and orig in pend),
                    "state": t["state"], "progress": t["progress"],
                    "ratio": t["ratio"], "seeding_days": t["seeding_time"] / 86400, "upspeed": t["upspeed"], "dlspeed": t["dlspeed"],
                    "size": t["size"], "added_on": t["added_on"], "rule": {k: r[k] for k in ("mode", "min_seed_days", "min_ratio")},
                    "rule_source": r["source"], "custom": h in ov, "rule_progress": progress,
                    "rule_met": met, "tracked": managed,
                    "days_met": t["seeding_time"] / 86400 >= r["min_seed_days"]
                    if r["mode"] not in ("ratio", "never") and r.get("min_seed_days") is not None else None,
                    "ratio_met": t["ratio"] >= r["min_ratio"] if r["mode"] not in ("days", "never") and r.get("min_ratio") is not None else None,
                    "in_library": lib, "leftover": managed and not lib and h not in pend and t["progress"] >= 1,
                    "queued": h in pend, "queued_as": name, "status": pend[h][1] if h in pend else None,
                    "delete_after": pend[h][3] + grace if h in pend and grace else None,
                    "undo": None if h not in pend else ("ok" if pend[h][2] and restorable(t) else "no")})
    return {"default": default_rule(c), "dry_run": bool(c.get("dry_run")), "modes": MODES, "torrents": out,
            "freed": {"total": freed[0], "count": freed[1], "last30": freed30}, "health": health,
            "version": VERSION, "update": update_info, "free_gb": free_gb({t["hash"]: t for t in raw})}


def tracker_choices(ts):
    """Suggestions for tracker rules: tracker names (from Sonarr/Radarr's grab history and Prowlarr), then announce hosts."""
    if not ts:
        return []
    names, hosts = tracker_names(ts), {}
    for t in ts:
        if host := tracker_host(t):
            hosts.setdefault(host, set()).add(names.get(t["hash"]))
    by_name = {}
    for t in ts:
        if n := names.get(t["hash"]):
            by_name.setdefault(n, set()).add(tracker_host(t))
    out = [{"value": n, "label": f"{n} · {', '.join(sorted(h - {''}))}"} for n, h in sorted(by_name.items(), key=lambda x: x[0].casefold())]
    out += [{"value": i["name"], "label": f"{i['name']} · Prowlarr, no torrents yet"} for i in indexer_info()["prowlarr"] if i["name"] not in by_name]
    out += [{"value": h, "label": h + (f" · {', '.join(sorted(n - {None}))}" if n - {None} else "")} for h, n in sorted(hosts.items())]
    return out


# ---------- connections, health, updates ----------

health = {}
update_info = {}
WEBHOOK_TEMPLATE = ("ewogICJub3RpZmljYXRpb25fdHlwZSI6ICJ7e2pzb25fZW5jb2RlIE5vdGlmaWNhdGlvblR5cGV9fSIsCiAgIml0ZW1fdHlwZSI6ICJ7e2pzb25fZW5jb2RlIEl0ZW1UeXBlfX0iLAogICJpdGVtX2lkIjogInt7anNvbl9lbmNvZGUgSXRlbUlkfX0iLAogICJuYW1lIjogInt7anNvbl9lbmNvZGUgTmFtZX19IiwKICAicGF0aCI6IG51bGwsCiAgInRtZGJfaWQiOiB7eyNpZl9leGlzdCBQcm92aWRlcl90bWRifX17e1Byb3ZpZGVyX3RtZGJ9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgInR2ZGJfaWQiOiB7eyNpZl9leGlzdCBQcm92aWRlcl90dmRifX17e1Byb3ZpZGVyX3R2ZGJ9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgImltZGJfaWQiOiB7eyNpZl9leGlzdCBQcm92aWRlcl9pbWRifX0ie3tqc29uX2VuY29kZSBQcm92aWRlcl9pbWRifX0ie3tlbHNlfX1udWxse3svaWZfZXhpc3R9fSwKICAic2VyaWVzX25hbWUiOiB7eyNpZl9leGlzdCBTZXJpZXNOYW1lfX0ie3tqc29uX2VuY29kZSBTZXJpZXNOYW1lfX0ie3tlbHNlfX1udWxse3svaWZfZXhpc3R9fSwKICAic2VyaWVzX2lkIjoge3sjaWZfZXhpc3QgU2VyaWVzSWR9fSJ7e2pzb25fZW5jb2RlIFNlcmllc0lkfX0ie3tlbHNlfX1udWxse3svaWZfZXhpc3R9fSwKICAic2Vhc29uX251bWJlciI6IHt7I2lmX2V4aXN0IFNlYXNvbk51bWJlcn19e3tTZWFzb25OdW1iZXJ9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgImVwaXNvZGVfbnVtYmVyIjoge3sjaWZfZXhpc3QgRXBpc29kZU51bWJlcn19e3tFcGlzb2RlTnVtYmVyfX17e2Vsc2V9fW51bGx7ey9pZl9leGlzdH19LAogICJlcGlzb2RlX2VuZF9udW1iZXIiOiB7eyNpZl9leGlzdCBFcGlzb2RlTnVtYmVyRW5kfX17e0VwaXNvZGVOdW1iZXJFbmR9fXt7ZWxzZX19bnVsbHt7L2lmX2V4aXN0fX0sCiAgIm9jY3VycmVkX2F0IjogInt7anNvbl9lbmNvZGUgVXRjVGltZXN0YW1wfX0iCn0=")


def test_service(svc, conf):
    """Can Shredarr reach this service with these settings? Returns a short description, or raises."""
    url = (conf or {}).get("url", "").strip().rstrip("/")
    if not url:
        raise RuntimeError("no address set")
    svc = kind_of(svc) if kind_of(svc) in ARR_KINDS else svc
    if svc in ("sonarr", "radarr", "prowlarr"):
        r = Arr({"url": api_url(svc, url), "api_key": conf.get("api_key", "")})("GET", "/system/status")
        return f"{r.get('appName', SERVICES[svc])} {r.get('version', '')}".strip()
    if svc in CLIENTS:
        return make_client(svc, {"username": "", "password": "", **conf, "url": url}).version()
    if svc == "plex":
        r = call("GET", url + "/", {"X-Plex-Token": conf.get("api_key", ""), "Accept": "application/json"})["MediaContainer"]
        return f"Plex {r.get('version', '').split('-')[0]} · {r.get('friendlyName', '')}"
    if svc == "seerr":
        seerr = Arr({"url": api_url(svc, url), "api_key": conf.get("api_key", "")})
        seerr("GET", "/settings/main")  # needs a valid API key
        return "Seerr " + (seerr("GET", "/status").get("version") or "")
    if svc == "jellyfin":
        r = call("GET", url + "/System/Info", {"X-Emby-Token": conf.get("api_key", "")})
        return f"Jellyfin {r.get('Version', '')} · {r.get('ServerName', '')}"
    raise RuntimeError("unknown service")


def connections(c):
    """[(key, name, conf)] of every app Shredarr uses: Sonarr/Radarr instances, the torrent client, the media server,
    and Seerr/Prowlarr if set up."""
    out = [(k, name, conf) for k, _, name, conf in arr_instances(c=c)]
    return out + [(k, SERVICES[k], c[k]) for k in (client_kind(c), media_kind(c), "seerr", "prowlarr") if (c.get(k) or {}).get("url")]


def check_health():
    c = cfg()
    conns = connections(c)
    for k in [k for k in health if k not in {x[0] for x in conns}]:
        health.pop(k, None)
    for svc, name, conf in conns:
        was = health.get(svc, {}).get("ok")
        try:
            info = test_service(svc, {**conf, "url": base_url(svc, conf["url"])})
            health[svc] = {"ok": True, "msg": info, "ts": time.time()}
            if was is False:
                log(f"Connection restored: {name}")
        except Exception as e:
            msg = f"HTTP {e.code}" + (" (check the API key)" if e.code in (401, 403) else "") if isinstance(e, urllib.error.HTTPError) else str(getattr(e, "reason", e))
            health[svc] = {"ok": False, "msg": msg, "ts": time.time(), "name": name}
            if was is not False:
                log(f"Connection FAILED: can't reach {name} ({msg})")


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
                    "redownloaded": r[9], "redownload_how": r[10], "cross_seed_of": (json.loads(r[8]) if r[8] else {}).get("cross_seed_of"),
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
        if info.get("cross_seed_of"):
            raise RuntimeError(f"it was a cross-seed of '{info['cross_seed_of']}'. Redownload that one instead")
        if info.get("movie") or info.get("series"):
            if kind_of(info.get("app")) not in ARR_KINDS or not any(x[0] == info["app"] for x in arr_instances()):
                info["app"] = "radarr" if info.get("movie") else "sonarr"  # older entry, or that instance was removed since
            return info
    info = {"hash": h, "name": name, "category": category, "movie": None, "series": None, "episodes": []}
    order = ("sonarr", "radarr") if re.search(r"tv|sonarr|show|series", category or "", re.I) else ("radarr", "sonarr")
    for app in order:
        try:
            arr = arr_api(app)
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
    app = arr_name(info["app"])
    if kind_of(info["app"]) == "radarr":
        what = f"'{info['movie']['title']}'" + (f" ({info['movie']['year']})" if info["movie"].get("year") else "")
        first = f"Put {what} back in {app} (with its previous settings if Shredarr has them) and monitor it"
        search = f"Have {app} search for it and grab the best release it finds"
    else:
        eps = episodes_text(info.get("episodes") or [])
        full = info.get("full_seasons") or []
        what = f"'{info['series']['title']}'" + (f" {eps}" if eps else (f" season {', '.join(map(str, full))}" if full else ""))
        first = f"Put '{info['series']['title']}' back in {app} if it was removed, and monitor {eps or 'those episodes'}"
        search = f"Have {app} search for " + (eps or "them") + " (whole seasons as season packs)"
    return {"what": what, "app": info["app"], "same_available": saved,
            "steps": {"same": [first, f"Add the original torrent back to {client_name()} ({info['name']}), so the same release downloads again",
                               f"When it finishes, it's imported into your library and shows up in {media_name()}"],
                      "search": [first, search, f"When it finishes, it's imported into your library and shows up in {media_name()}"]}}


def do_redownload(row_id, how):
    c = cfg()
    try:
        info = resolve_redownload(row_id)
    except Exception as e:
        return log(f"Redownload failed: {e}")
    is_movie = kind_of(info["app"]) == "radarr"
    title = info["movie"]["title"] if is_movie else info["series"]["title"]
    log(f"Redownload: {title}" + ("" if is_movie else f" {episodes_text(info.get('episodes') or [])}"))
    arr = arr_api(info["app"])
    try:
        if is_movie:
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
            tc("add", content, os.path.basename(path), info.get("category") or "")
            with db() as con:
                con.execute("insert or replace into redownloads (hash, ts, app, title, tries, last_try, done) values (?,?,?,?,0,0,0)",
                            (info["hash"], time.time(), info["app"], title))
            log(f"  done: added the original torrent back to {client_name()} ({info['name']})")
        else:
            if is_movie:
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
                Arr(c["seerr"])("POST", f"/settings/jobs/{kind_of(info['app'])}-scan/run")
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
            media_rescan([i["path"] for i in import_map(0).get(h, []) if i["path"] and os.path.exists(i["path"])])
            log(f"Redownload finished: '{title}' is back in your library")
            notify("Redownload finished", f"{title} is back in your library", "undo")
            continue
        if t["progress"] < 1 or time.time() - max(ts, last or 0) < 600 or tries >= 3:
            continue
        with db() as con:
            con.execute("update redownloads set tries=tries+1, last_try=? where hash=?", (time.time(), h))
        arr, kind = arr_api(app), "movie" if kind_of(app) == "radarr" else "tv"
        try:
            how = unpack_restore(arr, kind, t) if restorable(t) == "rar" else reimport(arr, kind, t)
            log(f"Redownload of '{title}' imported: {how}")
        except Exception as e:
            log(f"Redownload of '{title}': import not done yet ({e})")


# ---------- Plex: noticing deletes on disk ----------

WATCH_FILE = os.path.join(os.path.dirname(CFG), "library-files.json")
WATCH_CONFIRM = 100  # seconds a file must stay gone: an upgrade or rename replaces it within seconds
_watch = {"files": None, "missing": {}, "offline": False}
watch_skip = {}  # library path -> time: files Shredarr deleted itself (Delete now, No torrent), not reported as deletes


def watch_suppress(paths):
    now = time.time()
    for p in paths:
        watch_skip[p] = now
    for p in [p for p, t in watch_skip.items() if now - t > 86400]:
        watch_skip.pop(p, None)


def library_files():
    """path -> [device, inode] of every video file in the Sonarr/Radarr root folders, or None if one can't be read."""
    dirs = root_dirs()
    if not dirs or not all(os.path.isdir(d) for d in dirs):
        return None
    out = {}
    for root in dirs:
        for d, _, fs in os.walk(root):
            for p in videos_in(os.path.join(d, f) for f in fs):
                if k := file_key(p):
                    out[p] = list(k)
    return out


def watch_pass():
    """Plex can't tell Shredarr about deletes, so with Plex the library folders are watched instead. A video file that
    disappears and stays gone (it wasn't renamed, or replaced by an upgrade) is handled like a delete in Jellyfin."""
    now = library_files()
    if now is None:
        if not _watch["offline"]:
            log("Library watch paused: a Sonarr/Radarr root folder can't be read (check the /data mount)")
        _watch["offline"] = True
        return
    if _watch["offline"]:
        log("Library watch resumed: all Sonarr/Radarr root folders can be read again")
    _watch["offline"] = False
    prev = _watch["files"]
    if prev is None:
        try:
            with open(WATCH_FILE) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    gone = [p for p in (prev or {}) if p not in now]
    if prev and len(gone) > 20 and len(gone) > 0.2 * len(prev):
        # a disk or share that went away isn't a delete: keep the old list and look again next time
        return log(f"Library watch: {len(gone)} of {len(prev)} files vanished at once. That looks like a disk going offline, "
                   "not a delete, so nothing was done")
    _watch["files"] = now
    with open(WATCH_FILE + ".tmp", "w") as f:
        json.dump(now, f)
    os.replace(WATCH_FILE + ".tmp", WATCH_FILE)
    moved = {tuple(k) for k in now.values()}
    t = time.time()
    for p in gone:
        if tuple(prev[p]) not in moved and p not in watch_skip:
            _watch["missing"].setdefault(p, t)
    for p in list(_watch["missing"]):
        if p in now:
            _watch["missing"].pop(p)
    ready = [p for p, since in _watch["missing"].items() if t - since >= WATCH_CONFIRM]
    if ready:
        for p in ready:
            _watch["missing"].pop(p)
        events.put({"disk_deletes": ready})  # handled by the worker, one event at a time like Jellyfin's


def disk_deletes(paths):
    """Library files that were deleted outside Shredarr (in Plex): clean up the movies and episodes they were, unless
    Sonarr/Radarr already has another file for them (an upgrade)."""
    c = cfg()
    for key, kind, name, conf in arr_instances(c=c):
        a = Arr(conf)
        try:
            if kind == "radarr":
                movies = a("GET", "/movie")
                for m in movies:
                    root = local(m["path"]).rstrip("/") + "/"
                    if not any(p.startswith(root) for p in paths):
                        continue
                    if m.get("hasFile") and on_disk((m.get("movieFile") or {}).get("path")):
                        continue  # it has a (new) file, so this was an upgrade or a stray file
                    log(f"{media_name()} deleted Movie: {m['title']} (noticed on disk, {name})")
                    notify(f"Deleted in {media_name()}", f"Movie: {m['title']}", "jellyfin")
                    others = [x for k2, _, _, cf in arr_instances("radarr", c) if k2 != key
                              for x in Arr(cf)("GET", "/movie", tmdbId=m["tmdbId"]) if x.get("hasFile")]
                    movie_removed(c, key, name, a, m, seerr=not others)
            else:
                disk_episode_deletes(c, key, name, a, paths)
        except Exception as e:
            log(f"  error while handling deletes in {name}: {e!r}")


def disk_episode_deletes(c, key, name, sonarr, paths):
    for s in sonarr("GET", "/series"):
        root = local(s["path"]).rstrip("/") + "/"
        hit = [p for p in paths if p.startswith(root)]
        if not hit:
            continue
        files = {f["id"]: local(f["path"]) for f in sonarr("GET", "/episodefile", seriesId=s["id"])}
        eps = sonarr("GET", "/episode", seriesId=s["id"])
        gone = {}
        for p in hit:
            mine = [e for e in eps if e.get("episodeFileId") and files.get(e["episodeFileId"]) == p]
            if not mine:  # Sonarr already dropped the file record: work the episodes out from the file name
                try:
                    pr = sonarr("GET", "/parse", title=os.path.basename(p))
                    ids = {x["id"] for x in pr.get("episodes") or []}
                    mine = [e for e in eps if e["id"] in ids]
                except Exception:
                    mine = []
            for e in mine:
                if not (e.get("episodeFileId") and os.path.exists(files.get(e["episodeFileId"]) or "")):  # no newer file
                    gone[e["id"]] = (e, p)
        if not gone:
            continue
        if not os.path.isdir(local(s["path"])):
            log(f"{media_name()} deleted Series: {s['title']} (noticed on disk, {name})")
            notify(f"Deleted in {media_name()}", f"Series: {s['title']}", "jellyfin")
            series_removed(c, key, name, sonarr, s)
            continue
        for sn in sorted({e["seasonNumber"] for e, _ in gone.values()}):
            sel = sorted((e for e, _ in gone.values() if e["seasonNumber"] == sn), key=lambda e: e["episodeNumber"])
            # a whole season: its files were in a "Season N" folder, and that folder is gone too
            folders = {os.path.dirname(p) for e, p in gone.values() if e["seasonNumber"] == sn}
            season_gone = all(re.fullmatch(rf"Season 0*{sn}" + ("|Specials" if sn == 0 else ""), os.path.basename(d), re.I)
                              and not os.path.isdir(d) for d in folders)
            what = f"S{sn:02d}" if season_gone else episodes_label(sel)
            label = f"'{s['title']}' {what}"
            log(f"{media_name()} deleted {'Season' if season_gone else 'Episode'}: {s['title']} {what} (noticed on disk, {name})")
            notify(f"Deleted in {media_name()}", f"{s['title']} {what}", "jellyfin")
            remove_episodes(c, key, name, sonarr, s, sel, label, sn if season_gone else None)


def watcher():
    while True:
        try:
            c = cfg()
            if media_kind(c) == "plex" and not configured(c):
                watch_pass()
        except Exception as e:
            print("library watch:", e, flush=True)
        time.sleep(60)


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
    sub["%MEDIA%"], sub["%CLIENT%"] = media_name(), client_name()
    sub["%ASSETV%"] = f"{VERSION}.{int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC)))}"
    out = read_static("index.html")
    for k, v in sub.items():
        out = out.replace(k, v)
    return out



SETUP_BODY = """%ERR%<p class=small style="text-align:center;margin-bottom:14px">Welcome! Choose a password to protect Shredarr.
You'll connect Sonarr, Radarr, your torrent client and your media server next.</p>
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
                return self.reply(502, f"{client_name()}: {e}")
        if path == "/api/plan":
            h = query.get("hash", [""])[0]
            try:
                t, steps, _ = build_plan(cfg(), h)
            except Exception as e:
                return self.reply(502, f"could not build the plan: {e}")
            return self.reply(200, json.dumps({"name": t["name"] if t else h, "steps": [d for d, _ in steps],
                                               "notes": [d for d, fn in steps if fn is None],
                                               "dry_run": bool(cfg().get("dry_run"))}), "application/json")
        if path == "/api/orphans":
            try:
                o = media_without_torrent(0 if query.get("refresh") else 300)
            except Exception as e:
                return self.reply(502, f"couldn't check the library: {e}")
            return self.reply(200, json.dumps({"items": o["items"], "ts": o["t"]}), "application/json")
        if path == "/api/orphan-plan":
            try:
                steps, done, _ = orphan_plan(cfg(), query.get("ids", [""])[0].split(","))
            except Exception as e:
                return self.reply(502, f"could not build the plan: {e}")
            return self.reply(200, json.dumps({"steps": [d for d, _ in steps], "items": [x[0] for x in done],
                                               "size": sum(x[1] for x in done)}), "application/json")
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
            hide = lambda svc, conf: {k: ("" if k in SECRET_KEYS else (base_url(svc, v) if k == "url" else v)) for k, v in (conf or {}).items()} \
                | {"has_secret": any((conf or {}).get(k) for k in SECRET_KEYS)}
            conns = {svc: hide(svc, c.get(svc)) for svc in SERVICES}
            conns["extra_arrs"] = [hide(x["key"], x) for x in c.get("extra_arrs", [])]
            conns["torrent_client"], conns["media_server"] = client_kind(c), media_kind(c)
            try:
                ts = torrents() if not configured(c) else []
            except Exception:
                ts = []
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(":")[0]
            guess = f"http://{host or 'shredarr'}:8089"
            try:  # prefer the address Jellyfin's webhook already uses
                if media_kind(c) != "jellyfin":
                    raise RuntimeError
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
                                                           "trackers": tracker_choices(ts),
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
            if f.get("torrent_client") in CLIENTS:
                c["torrent_client"] = f["torrent_client"]
            if f.get("media_server") in MEDIA_SERVERS:
                c["media_server"] = f["media_server"]
            if isinstance(f.get("extra_arrs"), list):
                old, out, used = {x["key"]: x for x in c.get("extra_arrs", [])}, [], set(ARR_KINDS)
                for x in f["extra_arrs"]:
                    kind, url = x.get("kind"), str(x.get("url") or "").strip()
                    if kind not in ARR_KINDS or not url:
                        continue
                    name = str(x.get("name") or "").strip() or f"{SERVICES[kind]} 2"
                    key = x.get("key") if x.get("key") in old and kind_of(x["key"]) == kind else \
                        kind + "-" + (re.sub(r"^" + kind + "-?", "", re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")) or "2")
                    while key in used:
                        key += "-2"
                    used.add(key)
                    out.append({"key": key, "kind": kind, "name": name, "url": api_url(kind, url),
                                "api_key": str(x.get("api_key") or "").strip() or old.get(key, {}).get("api_key", "")})
                c["extra_arrs"] = out
            for svc, conf in f.items():
                if svc not in SERVICES or not isinstance(conf, dict):
                    continue
                old = c.get(svc) or {}
                new = {k: str(v).strip() for k, v in conf.items() if k in ("url", "api_key", "username", "password")}
                if not new.get("url"):
                    if svc in OPTIONAL:
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
            _imports["t"] = _titles["t"] = _indexers["t"] = _roots["t"] = _libidx["t"] = _orphans["t"] = 0
            log("Connection settings saved")
            threading.Thread(target=check_health, daemon=True).start()
            return self.reply(200, "ok")
        if path == "/api/test-connection":
            f, c = json.loads(body), cfg()
            svc, conf = f.get("service"), dict(f.get("conf") or {})
            saved = c.get(svc) or next((x for x in c.get("extra_arrs", []) if x.get("key") == svc), {})
            for k in SECRET_KEYS:
                if not conf.get(k) and saved.get(k):
                    conf[k] = saved[k]
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
        if path == "/api/orphans/delete":
            ids = [str(x) for x in json.loads(body).get("ids", [])]
            threading.Thread(target=delete_orphans, args=(ids,), daemon=True).start()
            return self.reply(202, "started")
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
    threading.Thread(target=watcher, daemon=True).start()
    if not cfg().get("webhook_token"):
        c0 = cfg()
        c0["webhook_token"] = secrets.token_hex(32)
        save_cfg(c0)
    log("Shredarr started")
    ThreadingHTTPServer(("0.0.0.0", 8089), Handler).serve_forever()
