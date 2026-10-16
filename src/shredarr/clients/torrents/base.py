"""What Shredarr needs from a torrent client, whichever one is used."""

from __future__ import annotations

from typing import Protocol, TypedDict


class Torrent(TypedDict):
    """A torrent, the same for every client (qBittorrent's names and states)."""

    hash: str  # lower case
    name: str
    save_path: str
    content_path: str  # the torrent's file, or its folder
    size: int
    progress: float  # 0..1
    ratio: float
    seeding_time: int  # seconds
    state: str  # qBittorrent state names: uploading, stalledUP, downloading, pausedUP, ...
    upspeed: int
    dlspeed: int
    added_on: int
    category: str
    tracker: str  # announce URL (contains the private passkey: never shown as is)


class TorrentClient(Protocol):
    def torrents(self, hashes: list[str] | None = None) -> list[Torrent]: ...

    def remove(self, hashes: list[str], with_data: bool) -> None: ...

    def export(self, h: str) -> bytes | None:
        """The torrent's .torrent file, if the client can hand it out."""
        ...

    def add(self, content: bytes, filename: str, category: str) -> None: ...

    def version(self) -> str:
        """e.g. "qBittorrent v5.1.2"; fails on wrong credentials."""
        ...
