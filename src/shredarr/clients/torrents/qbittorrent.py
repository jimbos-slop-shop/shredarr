"""qBittorrent, through qbittorrent-api (which signs in again by itself when the session expires)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import qbittorrentapi

from ...config import JSON
from ..base import TIMEOUT, ServiceError, ServiceHTTPError, ServiceUnreachable, SignInFailed
from .base import Torrent


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except qbittorrentapi.LoginFailed as e:
        raise SignInFailed("wrong username or password") from e
    except qbittorrentapi.HTTPError as e:
        raise ServiceHTTPError(getattr(e, "http_status_code", 0) or 0, str(e)) from e
    except qbittorrentapi.APIConnectionError as e:
        raise ServiceUnreachable(str(e)) from e
    except qbittorrentapi.APIError as e:
        raise ServiceError(str(e)) from e


class QBittorrentClient:
    def __init__(self, conf: JSON, local: Callable[[str], str] | None = None) -> None:
        self.api = qbittorrentapi.Client(
            host=conf["url"].rstrip("/"),
            username=conf.get("username", ""),
            password=conf.get("password", ""),
            REQUESTS_ARGS={"timeout": TIMEOUT},
            RAISE_NOTIMPLEMENTEDERROR_FOR_UNIMPLEMENTED_API_ENDPOINTS=False,
            DISABLE_LOGGING_DEBUG_OUTPUT=True,
        )

    @staticmethod
    def _torrent(t: Any) -> Torrent:
        return {
            "hash": t["hash"].lower(),
            "name": t["name"],
            "save_path": t["save_path"],
            "content_path": t["content_path"],
            "size": t["size"],
            "progress": t["progress"],
            "ratio": t["ratio"],
            "seeding_time": t["seeding_time"],
            "state": t["state"],
            "upspeed": t["upspeed"],
            "dlspeed": t["dlspeed"],
            "added_on": t["added_on"],
            "category": t["category"],
            "tracker": t["tracker"],
        }

    def torrents(self, hashes: list[str] | None = None) -> list[Torrent]:
        with _errors():
            return [self._torrent(t) for t in self.api.torrents_info(torrent_hashes=hashes or None)]

    def remove(self, hashes: list[str], with_data: bool) -> None:
        with _errors():
            self.api.torrents_delete(delete_files=with_data, torrent_hashes=hashes)

    def export(self, h: str) -> bytes | None:
        with _errors():
            data = self.api.torrents_export(torrent_hash=h)
        return data if isinstance(data, bytes) and data.startswith(b"d") else None

    def add(self, content: bytes, filename: str, category: str) -> None:
        with _errors():
            self.api.torrents_add(torrent_files={filename: content}, category=category or "")

    def close(self) -> None:
        """Nothing to do: qbittorrent-api has no public way to close its session."""

    def version(self) -> str:
        with _errors():
            return "qBittorrent " + str(self.api.app_version())
