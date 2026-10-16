"""Plex, through python-plexapi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer

from .base import TIMEOUT, ServiceHTTPError, ServiceUnreachable, refuse_if_read_only

T = TypeVar("T")


@dataclass(frozen=True)
class Section:
    key: str
    locations: list[str]


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token
        self._server: PlexServer | None = None

    def _call(self, fn: Callable[[PlexServer], T], fresh: bool = False) -> T:
        """Run fn on the server, turning plexapi's errors into Shredarr's. Connecting costs a request of its own, so
        the connection is kept for the next call, and made again after any error. fresh: connect again anyway
        (plexapi keeps what it read, such as the library sections, for as long as the connection lives)."""
        done, server = False, self._server  # read once: another thread may drop it after an error of its own
        try:
            if server is None or fresh:
                server = self._server = PlexServer(self.url, self.token, timeout=TIMEOUT)
            out = fn(server)
            done = True
            return out
        except Unauthorized as e:
            raise ServiceHTTPError(401, "Unauthorized") from e
        except NotFound as e:
            raise ServiceHTTPError(404, "Not Found") from e
        except BadRequest as e:
            raise ServiceHTTPError(400, str(e)) from e
        except requests.RequestException as e:
            raise ServiceUnreachable(str(e)) from e
        finally:
            if not done and self._server is server:  # don't drop a newer one another thread just made
                self._server = None

    def describe(self) -> str:
        return self._call(lambda s: f"Plex {s.version.split('-')[0]} · {s.friendlyName}", fresh=True)

    def sections(self) -> list[Section]:
        return self._call(lambda s: [Section(str(x.key), list(x.locations)) for x in s.library.sections()], fresh=True)

    def scan(self, section_key: str, path: str | None = None) -> None:
        """Scan a library section, or only one folder of it."""
        refuse_if_read_only("Plex")

        def run(s: PlexServer) -> None:
            key = f"/library/sections/{section_key}/refresh"
            s.query(key, params={"path": path} if path else None)

        self._call(run)

    def scan_all(self) -> None:
        for s in self.sections():
            self.scan(s.key)
