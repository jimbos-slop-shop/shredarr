"""A tiny threaded HTTP server with a route table, used to fake the apps Shredarr talks to.

Every request is recorded, so tests can assert on what Shredarr asked each app to do.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    params: dict[str, str] = field(default_factory=dict)  # named groups from the route pattern

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def form(self) -> dict[str, str]:
        return {k: v[-1] for k, v in urllib.parse.parse_qs(self.body.decode()).items()}

    def flag(self, name: str) -> bool:
        return self.query.get(name, "").lower() == "true"


@dataclass
class Response:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request], Any]


class FakeServer:
    """Routes are matched in order against the request path (without the query string)."""

    name = "fake"

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self.calls: list[Request] = []
        self.lock = threading.RLock()
        self._httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ routing
    def route(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.append((method, re.compile(pattern + r"/?$"), handler))

    def dispatch(self, req: Request) -> Response:
        for method, pattern, handler in self.routes:
            m = pattern.match(req.path)
            if m and method == req.method:
                req.params = m.groupdict()
                with self.lock:
                    out = handler(req)
                return out if isinstance(out, Response) else Response(200, out)
        return Response(404, {"message": f"{self.name}: no route for {req.method} {req.path}"})

    # ------------------------------------------------------------------ recording
    def requests(self, method: str | None = None, path: str | None = None) -> list[Request]:
        """Recorded requests, optionally filtered by method and a path regex (full match)."""
        return [r for r in list(self.calls)
                if (method is None or r.method == method) and (path is None or re.fullmatch(path, r.path))]

    def clear_calls(self) -> None:
        self.calls.clear()

    # ------------------------------------------------------------------ lifecycle
    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def start(self) -> FakeServer:
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = Request(self.command, parsed.path, {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()},
                              {k.lower(): v for k, v in self.headers.items()}, body)
                fake.calls.append(req)
                try:
                    resp = fake.dispatch(req)
                except Exception as e:  # a bug in a fake should show up as a clear failure, not a hang
                    resp = Response(500, {"message": f"{fake.name} fake crashed: {e!r}"})
                payload = resp.body
                ctype = resp.headers.get("Content-Type")
                if isinstance(payload, (dict, list)):
                    raw, ctype = json.dumps(payload).encode(), ctype or "application/json"
                elif payload is None:  # like the real apps: an empty body (e.g. after a DELETE)
                    raw, ctype = b"", ctype or "text/plain"
                elif isinstance(payload, bytes):
                    raw, ctype = payload, ctype or "application/octet-stream"
                else:
                    raw, ctype = str(payload if payload is not None else "").encode(), ctype or "text/plain"
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k != "Content-Type":
                        self.send_header(k, v)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

            def log_message(self, *a: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name=f"fake-{self.name}").start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
