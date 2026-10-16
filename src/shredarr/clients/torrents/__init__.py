"""Torrent clients behind one interface (TorrentClient), returning torrents in one shape (Torrent)."""

from __future__ import annotations

from collections.abc import Callable

from ...config import JSON
from .base import Torrent, TorrentClient
from .deluge import DelugeClient
from .qbittorrent import QBittorrentClient
from .transmission import TransmissionClient

CLIENT_CLASSES: dict[str, type[QBittorrentClient] | type[TransmissionClient] | type[DelugeClient]] = {
    "qbittorrent": QBittorrentClient, "transmission": TransmissionClient, "deluge": DelugeClient}


def make_client(kind: str, conf: JSON, local: Callable[[str], str] | None = None) -> TorrentClient:
    return CLIENT_CLASSES[kind](conf, local)


__all__ = ["Torrent", "TorrentClient", "make_client"]
