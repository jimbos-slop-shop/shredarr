"""Putting media back: re-adding it to Sonarr/Radarr and importing it again from a torrent's download.
Used by Undo (undo.py) and by Redownload (redownload.py)."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable

from ..clients.base import ServiceError, refuse_if_read_only
from ..clients.servarr import ArrClient, RadarrClient, SonarrClient
from ..clients.torrents import Torrent
from ..config import JSON, ConfigStore
from ..files import files_under
from .connections import Connections
from .library import Library
from .media_files import first_rar_volumes, restorable, videos_in
from .media_server import media_server

UNPACK_DIR = "_shredarr-restore"  # made next to the download, removed again after the import


def wait_for[T](fn: Callable[[], T], timeout: float = 120, every: float = 3) -> T | None:
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def most_used_profile(arr: ArrClient, items: list[JSON]) -> int:
    """The quality profile used by most of the library, for items Shredarr only knows by name."""
    used = [int(x["qualityProfileId"]) for x in items if x.get("qualityProfileId")]
    return max(set(used), key=used.count) if used else int(arr.quality_profiles()[0]["id"])


class Restorer:
    def __init__(self, config: ConfigStore, connections: Connections, library: Library) -> None:
        self.config = config
        self.connections = connections
        self.library = library

    # ------------------------------------------------------------------ back into Sonarr/Radarr
    def ensure_movie(self, radarr: RadarrClient, m: JSON) -> str:
        if radarr.movies(m["tmdbId"]):
            return "already in Radarr"
        for e in radarr.exclusions():
            if e.get("tmdbId") == m["tmdbId"]:
                radarr.delete_exclusion(e["id"])
        body = radarr.lookup_tmdb(m["tmdbId"])
        body.update({k: v for k, v in m.items() if v is not None})
        root = os.path.dirname(m["path"].rstrip("/")) if m.get("path") else radarr.root_folders()[0]["path"]
        body.update(rootFolderPath=root, addOptions={"searchForMovie": False, "monitor": "movieOnly"})
        if not body.get("qualityProfileId"):  # lookups return 0 for items not in the library yet
            body["qualityProfileId"] = most_used_profile(radarr, radarr.movies())
        radarr.add_movie(body)
        return "re-added to Radarr"

    def ensure_series(self, sonarr: SonarrClient, snap: JSON) -> str:
        s = snap["series"]
        if any(x["tvdbId"] == s["tvdbId"] for x in sonarr.all_series()):
            return "already in Sonarr"
        for e in sonarr.import_list_exclusions():
            if e.get("tvdbId") == s["tvdbId"]:
                sonarr.delete_import_list_exclusion(e["id"])
        body = sonarr.lookup(f"tvdb:{s['tvdbId']}")[0]
        body.update({k: v for k, v in s.items() if v is not None})
        root = os.path.dirname(s["path"].rstrip("/")) if s.get("path") else sonarr.root_folders()[0]["path"]
        body.update(rootFolderPath=root, addOptions={"monitor": "none", "searchForMissingEpisodes": False})
        if not body.get("qualityProfileId"):  # lookups return 0 for items not in the library yet
            body["qualityProfileId"] = most_used_profile(sonarr, sonarr.all_series())
        sid = sonarr.add_series(body)["id"]
        eps = wait_for(lambda: sonarr.episodes(sid), timeout=90) or []
        full, want = sonarr.series(sid), dict(map(tuple, snap["seasons"]))
        for x in full["seasons"]:
            x["monitored"] = bool(want.get(x["seasonNumber"], False))
        full["monitored"] = s.get("monitored", True)
        sonarr.update_series(full)
        on = {tuple(x) for x in snap["monitored_eps"]}
        ids = [e["id"] for e in eps if (e["seasonNumber"], e["episodeNumber"]) in on]
        if ids:
            sonarr.set_monitored(ids, True)
        return "re-added to Sonarr with its monitoring restored"

    @staticmethod
    def remonitor(sonarr: SonarrClient, snap: JSON) -> str:
        if snap["remonitor"]:
            sonarr.set_monitored(snap["remonitor"], True)
        if snap["seasons"]:
            sonarr.set_seasons_monitored(snap["series_id"], set(snap["seasons"]), True)
        return f"monitored {len(snap['remonitor'])} episode(s) again"

    # ------------------------------------------------------------------ importing from the download again
    def reimport(self, arr: ArrClient, kind: str, t: Torrent, path: str | None = None) -> str:
        """Ask Sonarr/Radarr to import from the torrent's download again (hardlinked, never moved), then check it
        landed."""
        cmd = arr.command(
            "DownloadedMoviesScan" if kind == "movie" else "DownloadedEpisodesScan",
            path=self.connections.paths().remote(path or t["content_path"]),
            downloadClientId=t["hash"].upper(),
            importMode="Copy",
        )
        finished = ("completed", "failed", "aborted", "cancelled")
        done = wait_for(lambda: (r := arr.command_status(cmd["id"]))["status"] in finished and r, timeout=300, every=4)
        self.library.forget(roots=False, index=False, groups=False, titles=False)
        self.library.forget_torrent(t["hash"])
        n = wait_for(lambda: self.library.in_library(t), timeout=30, every=3)
        if not n:
            raise ServiceError(
                f"nothing was imported ({(done or {}).get('status', 'timed out')}); see Activity → History in "
                f"{'Radarr' if kind == 'movie' else 'Sonarr'} for the reason"
            )
        media_server(self.connections).rescan(
            [i["path"] for i in self.library.imports(0).get(t["hash"], []) if i["path"] and os.path.exists(i["path"])]
        )
        return f"{n} file{'s' * (n != 1)} back in the library"

    def unpack(self, arr: ArrClient, kind: str, t: Torrent) -> str:
        """RAR release: unpack its archives (read in place, never changed) into a temporary folder beside the downloads,
        have Sonarr/Radarr import the video from there, then remove the temporary folder."""
        refuse_if_read_only("your downloads")
        if not self.config.load().restore_enabled:
            raise ServiceError("restoring RAR releases is turned off in Settings")
        if not shutil.which("unar"):
            raise ServiceError("the unpacker (unar) isn't installed in this container")
        src = t["content_path"]
        base = src if os.path.isdir(src) else os.path.dirname(src)
        st = os.stat(base)
        out_dir = os.path.join(os.path.dirname(base), UNPACK_DIR, re.sub(r"[^\w.\- ]", "_", t["name"]))
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            firsts = first_rar_volumes(files_under(src))
            if not firsts:
                raise ServiceError("couldn't find the first .rar volume in the torrent")
            for f in firsts:
                dest = os.path.join(out_dir, os.path.relpath(os.path.dirname(f), base))
                os.makedirs(dest, exist_ok=True)
                r = subprocess.run(
                    ["unar", "-q", "-f", "-D", "-o", dest, f], capture_output=True, text=True, timeout=3600, check=False
                )
                if r.returncode:
                    raise ServiceError("couldn't unpack it: " + (r.stderr or r.stdout).strip()[-300:])
            if not videos_in(files_under(out_dir)):
                raise ServiceError("the archive didn't contain a video file")
            for d, _, fs in os.walk(os.path.dirname(out_dir)):  # hand the files to the same owner as the downloads
                os.chown(d, st.st_uid, st.st_gid)
                for f in fs:
                    os.chown(os.path.join(d, f), st.st_uid, st.st_gid)
            return "unpacked, " + self.reimport(arr, kind, t, out_dir)
        finally:
            time.sleep(5)
            shutil.rmtree(out_dir, ignore_errors=True)
            with contextlib.suppress(OSError):
                os.rmdir(os.path.dirname(out_dir))  # only if no other restore is using it

    def restore(self, arr: ArrClient, kind: str, t: Torrent) -> str:
        """Import t again, unpacking it first if it's a RAR release."""
        return self.unpack(arr, kind, t) if restorable(t["content_path"]) == "rar" else self.reimport(arr, kind, t)

    def check_setup(self) -> tuple[bool, list[str]]:
        """Settings → Check setup: can this container restore a RAR release?"""
        if not self.config.load().restore_enabled:
            return True, ["Restoring RAR releases is turned off."]
        if not shutil.which("unar"):
            return False, ["The unpacker (unar) isn't installed in this container. Use the Shredarr image."]
        msgs = ["The unpacker (unar) is installed."]
        dirs = sorted(
            {
                os.path.dirname(t["save_path"].rstrip("/") + "/x")
                for t in self.connections.torrents.all()
                if os.path.isdir(t["save_path"])
            }
        )
        for d in dirs:
            probe = os.path.join(d, ".shredarr-probe")
            try:
                with open(probe, "w") as f:
                    f.write("probe")
                os.remove(probe)
            except OSError as e:
                return False, [*msgs, f"Shredarr can't write to {d} ({e.strerror}). Mount your downloads read-write."]
        msgs.append(f"Shredarr can write next to your downloads ({len(dirs)} folder{'s' * (len(dirs) != 1)}).")
        return True, msgs
