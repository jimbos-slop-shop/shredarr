"""Plex, through python-plexapi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer

from .base import TIMEOUT, ServiceHTTPError, ServiceUnreachable

T = TypeVar("T")


@dataclass(frozen=True)
class Section:
    key: str
    locations: list[str]


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    def _call(self, fn: Callable[[PlexServer], T]) -> T:
        """Connect and run fn, turning plexapi's errors into Shredarr's."""
        try:
            return fn(PlexServer(self.url, self.token, timeout=TIMEOUT))
        except Unauthorized as e:
            raise ServiceHTTPError(401, "Unauthorized") from e
        except NotFound as e:
            raise ServiceHTTPError(404, "Not Found") from e
        except BadRequest as e:
            raise ServiceHTTPError(400, str(e)) from e
        except requests.RequestException as e:
            raise ServiceUnreachable(str(e)) from e

    def describe(self) -> str:
        return self._call(lambda s: f"Plex {s.version.split('-')[0]} · {s.friendlyName}")

    def sections(self) -> list[Section]:
        return self._call(lambda s: [Section(str(x.key), list(x.locations)) for x in s.library.sections()])

    def scan(self, section_key: str, path: str | None = None) -> None:
        """Scan a library section, or only one folder of it."""
        def run(s: PlexServer) -> None:
            key = f"/library/sections/{section_key}/refresh"
            s.query(key, params={"path": path} if path else None)
        self._call(run)

    def scan_all(self) -> None:
        for s in self.sections():
            self.scan(s.key)
