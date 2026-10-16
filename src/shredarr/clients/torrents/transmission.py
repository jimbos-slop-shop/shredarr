"""Transmission, through transmission-rpc. Torrents come back in the same shape as qBittorrent's."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import transmission_rpc
from transmission_rpc.error import (
    TransmissionAuthError,
    TransmissionConnectError,
    TransmissionError,
    TransmissionTimeoutError,
)

from ...config import JSON
from ..base import TIMEOUT, ServiceError, ServiceUnreachable
from .base import Torrent

FIELDS = [
    "hashString",
    "name",
    "downloadDir",
    "sizeWhenDone",
    "percentDone",
    "uploadRatio",
    "secondsSeeding",
    "status",
    "error",
    "rateUpload",
    "rateDownload",
    "addedDate",
    "labels",
    "trackers",
    "torrentFile",
]
STATES = {1: "checkingUP", 2: "checkingUP", 3: "queuedDL", 4: "downloading", 5: "queuedUP"}  # 0 = stopped, 6 = seeding


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except TransmissionAuthError as e:
        raise ServiceError("wrong username or password") from e
    except (TransmissionConnectError, TransmissionTimeoutError) as e:
        raise ServiceUnreachable(str(e)) from e
    except TransmissionError as e:
        raise ServiceError(str(e)) from e


def state(f: JSON) -> str:
    done = f["percentDone"] >= 1
    if f.get("error"):
        return "error"
    if f["status"] == 0:
        return "pausedUP" if done else "pausedDL"
    if f["status"] == 6:
        return "uploading" if f["rateUpload"] else "stalledUP"
    return STATES.get(f["status"], "unknown")


class TransmissionClient:
    def __init__(self, conf: JSON, local: Callable[[str], str] | None = None) -> None:
        u = urllib.parse.urlsplit(conf["url"].rstrip("/"))
        path = u.path if u.path.endswith("/rpc") else u.path + "/transmission/rpc"
        self.local = local or (lambda p: p)
        with _errors():
            self.api = transmission_rpc.Client(
                protocol="https" if u.scheme == "https" else "http",
                host=u.hostname or "localhost",
                port=u.port or (443 if u.scheme == "https" else 80),
                path=path,
                username=conf.get("username") or None,
                password=conf.get("password") or None,
                timeout=TIMEOUT,
            )

    @staticmethod
    def _torrent(f: JSON) -> Torrent:
        labels = f.get("labels") or []
        trackers: list[Any] = f.get("trackers") or [{}]
        return {
            "hash": f["hashString"].lower(),
            "name": f["name"],
            "save_path": f["downloadDir"],
            "content_path": os.path.join(f["downloadDir"], f["name"]),
            "size": f["sizeWhenDone"],
            "progress": f["percentDone"],
            "ratio": max(0.0, f["uploadRatio"]),
            "seeding_time": f.get("secondsSeeding") or 0,
            "state": state(f),
            "upspeed": f["rateUpload"],
            "dlspeed": f["rateDownload"],
            "added_on": f["addedDate"],
            "category": labels[0] if labels else os.path.basename(f["downloadDir"].rstrip("/")),
            "tracker": trackers[0].get("announce", ""),
        }

    def _fields(self, hashes: list[str] | None) -> list[JSON]:
        with _errors():
            ids: list[int | str] | None = list(hashes) if hashes else None
            return [t.fields for t in self.api.get_torrents(ids=ids, arguments=FIELDS)]

    def torrents(self, hashes: list[str] | None = None) -> list[Torrent]:
        return [self._torrent(f) for f in self._fields(hashes)]

    def remove(self, hashes: list[str], with_data: bool) -> None:
        with _errors():
            self.api.remove_torrent(ids=list[int | str](hashes), delete_data=with_data)

    def export(self, h: str) -> bytes | None:
        """Transmission keeps .torrent files in its own config folder; readable only if that folder is mounted here."""
        fields = self._fields([h])
        p = self.local(fields[0].get("torrentFile") or "") if fields else ""
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def add(self, content: bytes, filename: str, category: str) -> None:
        with _errors():
            self.api.add_torrent(content, labels=[category] if category else None)

    def version(self) -> str:
        with _errors():
            return "Transmission " + str(self.api.get_session().version)
