"""The lab: a temporary disk with downloads and a media library, plus fake apps that all see it.

Builders (add_movie, add_series, add_release, add_cross_seed, ...) set things up the way Sonarr/Radarr and the torrent
client would have: real files, hardlinked library copies, arr history with download ids, and torrents.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from .media import FakeJellyfin, FakeNtfy, FakePlex, FakeSeerr, dashed, item_id
from .servarr import FakeProwlarr, FakeRadarr, FakeSonarr
from .torrents import FakeDeluge, FakeQBittorrent, FakeTransmission, Torrent, remove_data

DAY = 86400
TRACKER = "https://tracker.example.org/a1b2c3d4e5/announce"  # the path is a private passkey: it must never be shown
OTHER_TRACKER = "https://announce.othersite.net:2710/f0e9d8c7/announce"


def stable_int(text: str, digits: int = 6) -> int:
    return int(hashlib.sha1(text.encode()).hexdigest(), 16) % 10**digits + 10 ** (digits - 1)


def write(path: str, size: int) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    return path


@dataclass
class MovieRef:
    title: str
    year: int
    tmdb: int
    imdb: str
    folder: str  # library folder (on disk)
    file: str  # library file (on disk)
    radarr: FakeRadarr
    torrent: Torrent | None

    @property
    def id(self) -> int:
        return next(m["id"] for m in self.radarr.movies.values() if m["tmdbId"] == self.tmdb)

    @property
    def record(self) -> dict[str, Any]:
        return self.radarr.movies[self.id]


@dataclass
class SeriesRef:
    title: str
    tvdb: int
    tmdb: int
    imdb: str
    path: str  # show folder (on disk)
    flat: bool
    sonarr: FakeSonarr
    torrents: list[Torrent] = field(default_factory=list)

    @property
    def jellyfin_id(self) -> str:
        return item_id("series:" + self.title)

    @property
    def id(self) -> int:
        return next(s["id"] for s in self.sonarr.series.values() if s["tvdbId"] == self.tvdb)

    @property
    def record(self) -> dict[str, Any]:
        return self.sonarr.series[self.id]

    def season_dir(self, season: int) -> str:
        return self.path if self.flat else os.path.join(self.path, f"Season {season:02d}")

    def episode_file(self, season: int, number: int) -> str:
        return os.path.join(self.season_dir(season), f"{self.title} - S{season:02d}E{number:02d}.mkv")

    def episode(self, season: int, number: int) -> dict[str, Any]:
        return self.sonarr.episode(self.id, season, number)


class Lab:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        self.data = os.path.join(self.root, "data")
        self.downloads = os.path.join(self.data, "downloads")
        self.movies = os.path.join(self.data, "media", "movies")
        self.tv = os.path.join(self.data, "media", "tv")
        for d in (self.downloads, self.movies, self.tv):
            os.makedirs(d, exist_ok=True)
        self.app_prefix: tuple[str, str] | None = None  # (lab path, app path) when the apps mount the data elsewhere
        self._clock = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self.movie_catalog: dict[int, dict[str, Any]] = {}
        self.series_catalog: dict[int, dict[str, Any]] = {}
        self.releases: dict[str, dict[str, Any]] = {}  # hash -> what Sonarr/Radarr would import it as
        self.torrents: dict[str, Torrent] = {}
        self.gone: dict[str, dict[str, Any]] = {}  # removed torrents, so a re-added .torrent can download again
        self.auto_import = True
        self.movie_refs: dict[int, MovieRef] = {}
        self.series_refs: dict[int, SeriesRef] = {}
        self.sonarr = FakeSonarr(self, self.tv, "sonarr-key")
        self.radarr = FakeRadarr(self, self.movies, "radarr-key")
        self.prowlarr = FakeProwlarr()
        self.qbittorrent = FakeQBittorrent(self)
        self.transmission = FakeTransmission(self)
        self.deluge = FakeDeluge(self)
        self.jellyfin = FakeJellyfin(self)
        self.plex = FakePlex(self)
        self.seerr = FakeSeerr()
        self.ntfy = FakeNtfy()
        self.extra_arrs: list[tuple[str, FakeRadarr | FakeSonarr]] = []
        self.fakes = [
            self.sonarr,
            self.radarr,
            self.prowlarr,
            self.qbittorrent,
            self.transmission,
            self.deluge,
            self.jellyfin,
            self.plex,
            self.seerr,
            self.ntfy,
        ]
        for f in self.fakes:
            f.start()

    def stop(self) -> None:
        for f in self.fakes:
            f.stop()

    def clear_calls(self) -> None:
        for f in self.fakes:
            f.clear_calls()

    def clock(self) -> str:
        self._clock += dt.timedelta(minutes=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------ paths as the apps see them
    def app(self, p: str) -> str:
        if self.app_prefix and p.startswith(self.app_prefix[0]):
            return self.app_prefix[1] + p[len(self.app_prefix[0]) :]
        return p

    def local(self, p: str) -> str:
        if self.app_prefix and p.startswith(self.app_prefix[1]):
            return self.app_prefix[0] + p[len(self.app_prefix[1]) :]
        return p

    def local_from_jellyfin(self, p: str) -> str:
        pre = self.jellyfin.prefix
        return pre[0] + p[len(pre[1]) :] if pre and p.startswith(pre[1]) else p

    def mount_apps_at(self, app_root: str) -> None:
        """Make every app report paths under app_root instead of the lab's data folder (needs a path mapping)."""
        self.app_prefix = (self.data, app_root)
        self.sonarr.root_folder, self.radarr.root_folder = self.app(self.tv), self.app(self.movies)

    # ------------------------------------------------------------------ config for Shredarr
    def config(
        self,
        *,
        client: str = "qbittorrent",
        media: str = "jellyfin",
        seerr: bool = True,
        prowlarr: bool = False,
        notify: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        c: dict[str, Any] = {
            "sonarr": {"url": self.sonarr.url + "/api/v3", "api_key": self.sonarr.api_key},
            "radarr": {"url": self.radarr.url + "/api/v3", "api_key": self.radarr.api_key},
            "torrent_client": client,
            "media_server": media,
            "rules": {"mode": "either", "min_seed_days": 14, "min_ratio": 2},
            "update_check": False,
            "webhook_token": "lab-webhook-token",
        }
        if client == "qbittorrent":
            c["qbittorrent"] = {
                "url": self.qbittorrent.url,
                "username": self.qbittorrent.username,
                "password": self.qbittorrent.password,
            }
        elif client == "transmission":
            c["transmission"] = {"url": self.transmission.url, "username": "", "password": ""}
        else:
            c["deluge"] = {"url": self.deluge.url, "password": self.deluge.password}
        if media == "jellyfin":
            c["jellyfin"] = {"url": self.jellyfin.url, "api_key": self.jellyfin.api_key}
        else:
            c["plex"] = {"url": self.plex.url, "api_key": self.plex.token}
        if seerr:
            c["seerr"] = {"url": self.seerr.url + "/api/v1", "api_key": self.seerr.api_key}
        if prowlarr:
            c["prowlarr"] = {"url": self.prowlarr.url + "/api/v1", "api_key": "prowlarr-key"}
        if notify:
            c["notify"] = {"ntfy_server": self.ntfy.url, "ntfy_topic": "shredarr", "on_jellyfin": True}
        if self.extra_arrs:
            c["extra_arrs"] = [
                {
                    "key": key,
                    "kind": fake.app.lower(),
                    "name": f"{fake.app} 4K",
                    "url": fake.url + "/api/v3",
                    "api_key": fake.api_key,
                }
                for key, fake in self.extra_arrs
            ]
        if self.app_prefix:
            c["path_maps"] = [{"remote": self.app_prefix[1] + "/", "local": self.app_prefix[0] + "/"}]
        c.update(extra)
        return c

    def add_radarr_instance(self, key: str = "radarr-4k") -> FakeRadarr:
        folder = os.path.join(self.data, "media", "movies4k")
        os.makedirs(folder, exist_ok=True)
        fake = FakeRadarr(self, self.app(folder), "radarr4k-key").start()
        fake.name = key
        self.extra_arrs.append((key, fake))  # type: ignore[arg-type]
        self.fakes.append(fake)
        return fake  # type: ignore[return-value]

    # ------------------------------------------------------------------ torrents
    def add_torrent(
        self,
        name: str,
        disk_path: str,
        *,
        category: str,
        tracker: str = TRACKER,
        ratio: float = 0.0,
        seeding_days: float = 0.0,
        progress: float = 1.0,
        upspeed: int = 0,
        key: str | None = None,
    ) -> Torrent:
        h = hashlib.sha1(f"{key or name}|{tracker}".encode()).hexdigest()
        save = os.path.dirname(disk_path)
        t = Torrent(
            hash=h,
            name=name,
            save_path=self.app(save),
            content_path=self.app(disk_path),
            disk_path=disk_path,
            category=category,
            tracker=tracker,
            added_on=int(time.time() - seeding_days * DAY - 3600),
            ratio=ratio,
            seeding_time=int(seeding_days * DAY),
            progress=progress,
            upspeed=upspeed,
        )
        self.torrents[h] = t
        return t

    def remove_torrent(self, h: str, with_data: bool) -> None:
        t = self.torrents.pop(h, None)
        if not t:
            return
        files = (
            [t.disk_path]
            if os.path.isfile(t.disk_path)
            else [os.path.join(d, f) for d, _, fs in os.walk(t.disk_path) for f in fs]
        )
        self.gone[h] = {"torrent": t, "files": {p: os.path.getsize(p) for p in files if os.path.exists(p)}}
        if with_data:
            remove_data(t)

    def redownload(self, h: str, category: str) -> Torrent | None:
        """A re-added .torrent downloads its files again (fresh copies) and starts seeding from zero."""
        old = self.gone.pop(h, None)
        if not old or h in self.torrents:
            return self.torrents.get(h)
        for p, size in old["files"].items():
            if not os.path.exists(p):
                write(p, size)
        t: Torrent = old["torrent"]
        t.category, t.ratio, t.seeding_time, t.added_on = category or t.category, 0.0, 0, int(time.time())
        self.torrents[h] = t
        if self.auto_import:  # Sonarr/Radarr's completed download handling imports finished downloads on their own
            release = self.releases.get(h) or {}
            arr = self.radarr if release.get("kind") == "movie" else self.sonarr
            arr.import_download(t.disk_path, h)
        return t

    # ------------------------------------------------------------------ movies
    def add_movie(
        self,
        title: str,
        year: int,
        *,
        release: str | None = None,
        category: str = "radarr",
        ratio: float = 0.0,
        seeding_days: float = 0.0,
        tracker: str = TRACKER,
        indexer: str = "ExampleTracker",
        rar: bool = False,
        radarr: FakeRadarr | None = None,
        seerr: bool = True,
        size: int = 3000,
        torrent: bool = True,
    ) -> MovieRef:
        radarr = radarr or self.radarr
        tmdb, imdb = stable_int("movie:" + title), f"tt{stable_int('imdb:' + title, 7)}"
        self.movie_catalog[tmdb] = {"title": title, "year": year, "imdb": imdb}
        release = release or f"{title.replace(' ', '.')}.{year}.1080p.BluRay.x264-LAB"
        folder = os.path.join(self.local(radarr.root_folder), f"{title} ({year})")
        lib = os.path.join(folder, f"{title} ({year}) [Bluray-1080p].mkv")
        m = radarr.create_movie(tmdb, title, year, self.app(folder))
        t = None
        if torrent:
            dl = os.path.join(self.downloads, category, release)
            if rar:
                for ext in ("rar", "r00", "r01"):
                    write(os.path.join(dl, f"{release.lower()}.{ext}"), size // 3)
                write(lib, size)  # unpacked by Unpackerr: not linked to the torrent
                src = os.path.join(dl, f"{release.lower()}.rar")
            else:
                src = write(os.path.join(dl, release + ".mkv"), size)
                write(os.path.join(dl, release + ".nfo"), 10)
                os.makedirs(folder, exist_ok=True)
                os.link(src, lib)
            t = self.add_torrent(
                release, dl, category=category, tracker=tracker, ratio=ratio, seeding_days=seeding_days
            )
            self.releases[t.hash] = {"kind": "movie", "tmdb": tmdb, "name": release}
            radarr.add_history("grabbed", t.hash, release, {"indexer": f"{indexer} (Prowlarr)"}, movieId=m["id"])
            radarr.add_history(
                "downloadFolderImported",
                t.hash,
                release,
                {"importedPath": self.app(lib), "droppedPath": self.app(src)},
                movieId=m["id"],
            )
        else:
            write(lib, size)
        radarr.set_movie_file(m, lib)
        if seerr:
            self.seerr.add("movie", tmdb)
        ref = MovieRef(title, year, tmdb, imdb, folder, lib, radarr, t)
        self.movie_refs[tmdb] = ref
        return ref

    def movie_by_folder(self, folder: str) -> MovieRef | None:
        return next((m for m in self.movie_refs.values() if m.folder == folder), None)

    # ------------------------------------------------------------------ shows
    def add_series(
        self,
        title: str,
        *,
        year: int = 2020,
        seasons: dict[int, int] | None = None,
        flat: bool = False,
        unaired: tuple[tuple[int, int], ...] = (),
        seerr: bool = True,
    ) -> SeriesRef:
        tvdb, tmdb, imdb = (
            stable_int("tvdb:" + title),
            stable_int("tmdb:" + title),
            f"tt{stable_int('imdb:' + title, 7)}",
        )
        seasons = seasons or {1: 6}
        self.series_catalog[tvdb] = {
            "title": title,
            "year": year,
            "tmdb": tmdb,
            "imdb": imdb,
            "seasons": {
                sn: [
                    {
                        "number": n,
                        "title": f"Episode {n}",
                        "tvdb": stable_int(f"ep:{title}:{sn}:{n}", 7),
                        "air": "2099-01-01T00:00:00Z"
                        if (sn, n) in unaired
                        else f"2025-0{min(sn, 9)}-{n:02d}T02:00:00Z",
                    }
                    for n in range(1, count + 1)
                ]
                for sn, count in seasons.items()
            },
        }
        path = os.path.join(self.tv, title)
        os.makedirs(path, exist_ok=True)
        self.sonarr.create_series(tvdb, self.app(path), season_folder=not flat)
        if seerr:
            self.seerr.add("tv", tmdb)
        ref = SeriesRef(title, tvdb, tmdb, imdb, path, flat, self.sonarr)
        self.series_refs[tvdb] = ref
        return ref

    def series_by_path(self, path: str) -> SeriesRef | None:
        return next((s for s in self.series_refs.values() if path.startswith(s.path + "/")), None)

    def add_release(
        self,
        s: SeriesRef,
        season: int,
        episodes: list[int] | range,
        *,
        release: str | None = None,
        single_file: bool = False,
        category: str = "tv-sonarr",
        ratio: float = 0.0,
        seeding_days: float = 0.0,
        tracker: str = TRACKER,
        indexer: str = "ExampleTracker",
        size: int = 2000,
        rar: bool = False,
    ) -> Torrent:
        """A torrent with these episodes, imported into the show (replacing older files: an upgrade)."""
        eps = list(episodes)
        dotted = s.title.replace(" ", ".")
        release = release or (
            f"{dotted}.S{season:02d}E{eps[0]:02d}.1080p.WEB-DL-LAB"
            if len(eps) == 1
            else f"{dotted}.S{season:02d}.1080p.WEB-DL-LAB"
        )
        base = os.path.join(self.downloads, category)
        disk = os.path.join(base, release + ".mkv") if single_file else os.path.join(base, release)
        t = self.add_torrent(release, disk, category=category, tracker=tracker, ratio=ratio, seeding_days=seeding_days)
        self.releases[t.hash] = {"kind": "episodes", "tvdb": s.tvdb, "name": release}
        if rar:
            for ext in ("rar", "r00"):
                write(os.path.join(disk, f"{release.lower()}.{ext}"), size)
        for n in eps:
            src = disk if single_file else os.path.join(disk, f"{dotted}.S{season:02d}E{n:02d}.1080p.WEB-DL-LAB.mkv")
            lib = s.episode_file(season, n)
            if os.path.exists(lib):
                os.remove(lib)  # upgrade: the new file replaces the old one in the library
            if rar:
                write(lib, size)
                src = os.path.join(disk, f"{release.lower()}.rar")
            else:
                write(src, size)
                os.makedirs(os.path.dirname(lib), exist_ok=True)
                os.link(src, lib)
            e = s.episode(season, n)
            s.sonarr.set_episode_file(s.record, [e], lib)
            s.sonarr.add_history(
                "grabbed", t.hash, release, {"indexer": f"{indexer} (Prowlarr)"}, seriesId=s.id, episodeId=e["id"]
            )
            s.sonarr.add_history(
                "downloadFolderImported",
                t.hash,
                release,
                {"importedPath": self.app(lib), "droppedPath": self.app(src)},
                seriesId=s.id,
                episodeId=e["id"],
            )
        if not single_file:
            write(os.path.join(disk, release + ".nfo"), 10)
        s.torrents.append(t)
        return t

    def add_library_episodes(self, s: SeriesRef, season: int, episodes: list[int] | range, size: int = 2000) -> None:
        """Episodes in the library that no torrent seeds (e.g. the torrent was removed long ago)."""
        for n in episodes:
            lib = write(s.episode_file(season, n), size)
            s.sonarr.set_episode_file(s.record, [s.episode(season, n)], lib)

    # ------------------------------------------------------------------ other torrents
    def add_cross_seed(
        self,
        t: Torrent,
        *,
        tracker: str = OTHER_TRACKER,
        same_path: bool = False,
        ratio: float = 0.0,
        seeding_days: float = 0.0,
    ) -> Torrent:
        """The same release from another tracker: seeding from the same folder, or from hardlinks to the same files."""
        if same_path:
            disk = t.disk_path
        else:
            disk = os.path.join(self.downloads, "cross-seeds", os.path.basename(t.disk_path))
            if os.path.isfile(t.disk_path):
                os.makedirs(os.path.dirname(disk), exist_ok=True)
                os.link(t.disk_path, disk)
            else:
                for d, _, fs in os.walk(t.disk_path):
                    for f in fs:
                        dest = os.path.join(disk, os.path.relpath(os.path.join(d, f), t.disk_path))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        os.link(os.path.join(d, f), dest)
        x = self.add_torrent(
            t.name, disk, category="cross-seed", tracker=tracker, ratio=ratio, seeding_days=seeding_days
        )
        x.category = t.category if same_path else "cross-seed"
        return x

    def add_unmanaged(self, name: str, files: list[str], *, category: str = "courses", **kw: Any) -> Torrent:
        """A torrent Sonarr/Radarr never saw (e.g. courses the media server reads straight from the download folder)."""
        disk = os.path.join(self.downloads, category, name)
        for f in files:
            write(os.path.join(disk, f), 500)
        return self.add_torrent(name, disk, category=category, **kw)

    # ------------------------------------------------------------------ Jellyfin webhook payloads
    def event(self, item_type: str, **kw: Any) -> dict[str, Any]:
        base = {
            "notification_type": "ItemDeleted",
            "item_type": item_type,
            "item_id": None,
            "name": None,
            "path": None,
            "tmdb_id": None,
            "tvdb_id": None,
            "imdb_id": None,
            "series_name": None,
            "series_id": None,
            "season_number": None,
            "episode_number": None,
            "episode_end_number": None,
            "occurred_at": "2026-10-16T00:00:00.0000000Z",
        }
        return {**base, **kw}

    def movie_event(self, m: MovieRef) -> dict[str, Any]:
        return self.event(
            "Movie",
            item_id=dashed(item_id(self.jellyfin.jf_path(m.file))),
            name=m.title,
            tmdb_id=m.tmdb,
            imdb_id=m.imdb,
        )

    def series_event(self, s: SeriesRef) -> dict[str, Any]:
        return self.event(
            "Series", item_id=dashed(s.jellyfin_id), name=s.title, tvdb_id=s.tvdb, tmdb_id=s.tmdb, imdb_id=s.imdb
        )

    def season_event(self, s: SeriesRef, season: int) -> dict[str, Any]:
        return self.event(
            "Season",
            item_id=dashed(item_id(f"season:{s.title}:{season}")),
            name=f"Season {season}",
            series_name=s.title,
            series_id=dashed(s.jellyfin_id),
            season_number=season,
        )

    def episode_event(self, s: SeriesRef, season: int, number: int, end: int | None = None) -> dict[str, Any]:
        info = self.series_catalog[s.tvdb]["seasons"][season][number - 1]
        return self.event(
            "Episode",
            item_id=dashed(item_id(self.jellyfin.jf_path(s.episode_file(season, number)))),
            name=info["title"],
            tvdb_id=info["tvdb"],
            series_name=s.title,
            series_id=dashed(s.jellyfin_id),
            season_number=season,
            episode_number=number,
            episode_end_number=end,
        )

    def event_for_item(self, it: dict[str, Any]) -> dict[str, Any]:
        """What Jellyfin's Webhook plugin sends after an item is deleted through its API."""
        if it["Type"] == "Movie":
            ids = it.get("ProviderIds") or {}
            return self.event(
                "Movie",
                item_id=dashed(it["Id"]),
                name=it["Name"],
                tmdb_id=int(ids["Tmdb"]) if ids.get("Tmdb") else None,
                imdb_id=ids.get("Imdb"),
            )
        return self.event(
            "Episode",
            item_id=dashed(it["Id"]),
            name=it["Name"],
            series_name=it.get("SeriesName"),
            series_id=dashed(it["SeriesId"]) if it.get("SeriesId") else None,
            season_number=it.get("ParentIndexNumber"),
            episode_number=it.get("IndexNumber"),
        )

    # ------------------------------------------------------------------ deleting in the media server
    def delete_movie_files(self, m: MovieRef) -> None:
        shutil.rmtree(m.folder, ignore_errors=True)

    def delete_episode_files(self, s: SeriesRef, season: int, numbers: list[int] | range) -> None:
        for n in numbers:
            p = s.episode_file(season, n)
            if os.path.exists(p):
                os.remove(p)

    def delete_season_folder(self, s: SeriesRef, season: int) -> None:
        shutil.rmtree(s.season_dir(season), ignore_errors=True)

    def delete_series_folder(self, s: SeriesRef) -> None:
        shutil.rmtree(s.path, ignore_errors=True)

    def library_videos(self) -> list[str]:
        return sorted(
            os.path.relpath(os.path.join(d, f), self.data)
            for root in (self.movies, self.tv)
            for d, _, fs in os.walk(root)
            for f in fs
            if re.search(r"\.(mkv|mp4)$", f)
        )
