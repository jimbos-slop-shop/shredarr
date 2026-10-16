"""Sign-in: one password (PBKDF2 hash in config.json), 30-day sessions, and one-time reset codes."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time

from ..config import JSON, ConfigStore
from ..repositories import SessionsRepo
from .activity import Activity
from .notifications import Notifier

log = logging.getLogger(__name__)

SESSION_DAYS = 30
PBKDF2_ROUNDS = 240_000
RESET_VALID = 900  # seconds
RESET_EVERY = 60  # seconds between reset codes
RESET_TRIES = 5
LOGIN_WINDOW, LOGIN_TRIES = 900, 10  # at most this many wrong passwords per address in this many seconds


def hash_password(pw: str, salt: str | None = None) -> JSON:
    salt = salt or secrets.token_hex(16)
    return {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()}


class Auth:
    def __init__(
        self, config: ConfigStore, sessions: SessionsRepo, reset_file: str, activity: Activity, notifier: Notifier
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.reset_file = reset_file
        self.activity = activity
        self.notifier = notifier
        self._reset: JSON = {"code": None, "expires": 0, "made": 0, "tries": 0}
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the password
    @property
    def has_password(self) -> bool:
        return bool(self.config.load().get("auth"))

    def check(self, pw: str) -> bool:
        a = self.config.load().get("auth")
        return bool(a) and hmac.compare_digest(hash_password(pw, a["salt"])["hash"], a["hash"])

    def set_password(self, pw: str) -> None:
        self.config.update(lambda raw: raw.update(auth=hash_password(pw)))

    def policy_errors(self, pw: str) -> list[str]:
        """What the password still needs, e.g. ["at least 8 characters", "a number"]."""
        p, errs = self.config.load().password_policy, []
        n = max(1, int(p["min_length"]))
        if len(pw) < n:
            errs.append(f"at least {n} character{'s' * (int(p['min_length']) > 1)}")
        if p["upper"] and not re.search(r"[A-Z]", pw):
            errs.append("an uppercase letter")
        if p["digit"] and not re.search(r"\d", pw):
            errs.append("a number")
        if p["symbol"] and not re.search(r"[^A-Za-z0-9]", pw):
            errs.append("a symbol")
        return errs

    # ------------------------------------------------------------------ sessions
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions.add(token, SESSION_DAYS)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token) and self.sessions.valid(token or "")

    # ------------------------------------------------------------------ wrong passwords
    def login_blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            self._fails[ip] = [x for x in self._fails.get(ip, []) if now - x < LOGIN_WINDOW]
            return len(self._fails[ip]) >= LOGIN_TRIES

    def login_failed(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def login_ok(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    # ------------------------------------------------------------------ reset codes
    def make_reset_code(self) -> bool:
        """A one-time code, valid 15 minutes. Only someone who can read the server's files, its log or the configured
        notification channels can see it. False if one was made less than a minute ago."""
        with self._lock:
            if time.time() - self._reset["made"] < RESET_EVERY:
                return False
            code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
            self._reset.update(code=code, expires=time.time() + RESET_VALID, made=time.time(), tries=0)
        with open(self.reset_file, "w") as f:
            f.write(
                f"Shredarr password reset code: {code}\n"
                f"Valid until {time.strftime('%H:%M', time.localtime(self._reset['expires']))}.\n"
            )
        os.chmod(self.reset_file, 0o600)
        log.warning("[reset] password reset code: %s (valid 15 minutes)", code)
        self.activity.info("Password reset code requested")
        self.notifier.send_in_background(
            self.config.load().notify, "Shredarr password reset", f"Your reset code is {code}. It works for 15 minutes."
        )
        return True

    def use_reset_code(self, code: str) -> bool:
        with self._lock:
            r = self._reset
            if not r["code"] or time.time() > r["expires"] or r["tries"] >= RESET_TRIES:
                return False
            r["tries"] += 1
            if not hmac.compare_digest(code.strip().upper(), r["code"]):
                return False
            r.update(code=None, expires=0)
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.reset_file)
        return True
