"""The HTML pages: the dashboard/settings page (static/index.html) and the sign-in pages (static/login.html)."""

from __future__ import annotations

import html as htmlmod
import json
import os

from ..app import Shredarr
from ..config import MODES

CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".html": "text/html",
}
ALIASES = {"/logo.svg": "logo.svg", "/logo.png": "logo.png", "/favicon.ico": "logo.png"}

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


def static_file(static_dir: str, path: str) -> tuple[bytes, str] | None:
    """(content, content type) of a file the browser may load without signing in, or None."""
    name = ALIASES.get(path) or (path[len("/static/") :] if path.startswith("/static/") else None)
    if not name or "/" in name or name == "index.html" or not os.path.isfile(os.path.join(static_dir, name)):
        return None
    with open(os.path.join(static_dir, name), "rb") as f:
        return f.read(), CONTENT_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")


def read(static_dir: str, name: str) -> str:
    with open(os.path.join(static_dir, name), encoding="utf-8") as f:
        return f.read()


def main_page(core: Shredarr) -> str:
    c = core.config.load()
    r = c.default_rule
    static = core.settings.static_dir

    def fmt(v: float | None) -> str:
        return "" if v is None else f"{v:g}"

    dry = c.dry_run
    sub = {
        "%BANNERHIDDEN%": "" if dry else "hidden",
        "%MODECLS%": "dry" if dry else "live",
        "%MODETXT%": "Dry run" if dry else "Live",
        "%DAYS%": fmt(r["min_seed_days"]),
        "%RATIO%": fmt(r["min_ratio"]),
        "%MODEOPTS%": "".join(
            f"<option value='{k}'{' selected' if k == r['mode'] else ''}>{htmlmod.escape(v)}</option>"
            for k, v in MODES.items()
        ),
        "%DRYCHK%": "checked" if dry else "",
        "%MODESJSON%": json.dumps(MODES),
        "%VERSION%": core.version,
        "%MEDIA%": c.media_name,
        "%CLIENT%": c.client_name,
        "%ASSETV%": f"{core.version}.{int(max(os.path.getmtime(os.path.join(static, f)) for f in os.listdir(static)))}",
    }
    out = read(static, "index.html")
    for k, v in sub.items():
        out = out.replace(k, v)
    return out


def sign_in_page(core: Shredarr, subtitle: str, body: str, note: str = "", ok: bool = False) -> str:
    message = f"<div class={'ok' if ok else 'err'}>{htmlmod.escape(note)}</div>" if note else ""
    return (
        read(core.settings.static_dir, "login.html")
        .replace("%SUB%", subtitle)
        .replace("%BODY%", body.replace("%ERR%", message))
    )


def setup_page(core: Shredarr, err: str = "") -> str:
    return sign_in_page(core, "First-time setup", SETUP_BODY, err)


def login_page(core: Shredarr, err: str = "") -> str:
    return sign_in_page(core, "Sign in to manage cleanups", LOGIN_BODY, err)


def reset_page(core: Shredarr, msg: str = "", ok: bool = False) -> str:
    return sign_in_page(core, "Reset your password", RESET_BODY, msg, ok)
