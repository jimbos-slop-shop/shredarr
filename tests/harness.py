"""Runs Shredarr as a real process against the lab and talks to it over HTTP, the way a browser and Jellyfin do.

Tests only use Shredarr's public surface (web API, webhook, config file, database, CLI), so they describe what Shredarr
does, not how. (They were first run against Shredarr 1.2.1, and the expected output in tests/golden comes from it.)
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBHOOK_TOKEN = "lab-webhook-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def lan_ip() -> str | None:
    """An address of this machine other than 127.0.0.1, to reach Shredarr as a non-local client (sign-in applies)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = str(s.getsockname()[0])
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def wait_for[T](fn: Callable[[], T], timeout: float = 15, every: float = 0.1, what: str = "condition") -> T:
    end = time.time() + timeout
    last: Any = None
    while time.time() < end:
        try:
            last = fn()
        except (AssertionError, KeyError, IndexError, httpx.HTTPError) as e:
            last = e
        else:
            if last:
                return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


COMMAND = [sys.executable, "-m", "shredarr"]


class Shredarr:
    def __init__(self, workdir: str, config: dict[str, Any] | None, env: dict[str, str] | None = None) -> None:
        self.config_dir = os.path.join(workdir, "config")
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_path = os.path.join(self.config_dir, "config.json")
        self.db_path = os.path.join(self.config_dir, "state.db")
        if config is not None:
            self.write_config(config)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "SHREDARR_CONFIG": self.config_path,
            "SHREDARR_DB": self.db_path,
            "SHREDARR_PORT": str(self.port),
            "SHREDARR_WATCH_INTERVAL": "0.5",
            "SHREDARR_WATCH_CONFIRM": "1.5",
            "PYTHONUNBUFFERED": "1",
            "TZ": "UTC",
            **(env or {}),
        }
        self.output_path = os.path.join(workdir, "shredarr.out")
        self.proc: subprocess.Popen[bytes] | None = None
        self.http = httpx.Client(base_url=self.base, timeout=60, follow_redirects=False)

    # ------------------------------------------------------------------ process
    def start(self) -> Shredarr:
        out = open(self.output_path, "ab")  # noqa: SIM115 (closed with the process)
        self.proc = subprocess.Popen(COMMAND, env=self.env, stdout=out, stderr=subprocess.STDOUT, cwd=ROOT)
        end = time.time() + 30
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Shredarr exited with {self.proc.returncode}:\n{self.output()}")
            try:
                if self.http.get("/health").status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"Shredarr didn't start:\n{self.output()}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self) -> Shredarr:
        self.stop()
        return self.start()

    def output(self) -> str:
        with open(self.output_path, errors="replace") as f:
            return f.read()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*COMMAND, *args],
            env=self.env,
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
            check=False,
        )

    # ------------------------------------------------------------------ files
    def write_config(self, config: dict[str, Any]) -> None:
        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=2)

    def read_config(self) -> dict[str, Any]:
        with open(self.config_path) as f:
            return dict(json.load(f))

    def sql(self, query: str, *args: Any) -> list[tuple[Any, ...]]:
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            return con.execute(query, args).fetchall()
        finally:
            con.close()

    # ------------------------------------------------------------------ HTTP
    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.http.get(path, **kw)

    def api(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params)
        assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text}"
        return r.json()

    def post(
        self,
        path: str,
        json_body: Any = None,
        *,
        data: Any = None,
        headers: dict[str, str] | None = None,
        fetch: bool = True,
    ) -> httpx.Response:
        h = {"X-Fetch": "1"} if fetch else {}
        return self.http.post(path, json=json_body, data=data, headers={**h, **(headers or {})})

    def webhook(self, payload: Any, token: str | None = WEBHOOK_TOKEN) -> httpx.Response:
        return self.http.post("/webhook/jellyfin", json=payload, headers={"X-Webhook-Token": token} if token else {})

    # ------------------------------------------------------------------ activity log
    def log(self) -> list[str]:
        return [m for _, m in reversed(self.api("/api/log"))]

    def log_since(self, mark: int) -> list[str]:
        return self.log()[mark:]

    def wait_log(self, pattern: str, timeout: float = 20, after: int = 0) -> str:
        """Wait for a log line matching pattern; with after=n, only lines after the first n count."""
        rx = re.compile(pattern)
        return wait_for(
            lambda: next((m for m in self.log()[after:] if rx.search(m)), None), timeout, 0.15, f"log line /{pattern}/"
        )

    def pending(self) -> dict[str, dict[str, Any]]:
        rows = self.sql("select hash, name, source, status, undo, event, content from pending")
        return {
            r[0]: {
                "name": r[1],
                "source": r[2],
                "status": r[3],
                "undo": json.loads(r[4]) if r[4] else None,
                "event": r[5],
                "content": json.loads(r[6]) if r[6] else None,
            }
            for r in rows
        }

    def wait_status(self, h: str, pattern: str, timeout: float = 20) -> str:
        rx = re.compile(pattern)
        return wait_for(
            lambda: (s := (self.pending().get(h) or {}).get("status")) and rx.search(s) and s,
            timeout,
            0.15,
            f"status /{pattern}/ of {h[:8]} (pending: {self.pending().get(h)})",
        )

    def sweep(self) -> None:
        r = self.post("/sweep")
        assert r.status_code == 200, r.text
