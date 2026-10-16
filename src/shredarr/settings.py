"""Process settings, from environment variables. Everything users change lives in config.json (see config.py)."""

from __future__ import annotations

import os
from dataclasses import dataclass

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@dataclass(frozen=True)
class Settings:
    config_path: str = "/config/config.json"
    db_path: str = "/config/state.db"
    host: str = "0.0.0.0"
    port: int = 8089
    repo: str = "jimbos-slop-shop/shredarr"  # where update checks look for new releases
    watch_interval: float = 60  # Plex library watch: seconds between looks at the library folders
    watch_confirm: float = 100  # seconds a file must stay gone: an upgrade or rename replaces it within seconds
    read_only: bool = False  # a test copy: never change anything in another app, send no notifications
    static_dir: str = STATIC_DIR

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            config_path=env("SHREDARR_CONFIG", cls.config_path),
            db_path=env("SHREDARR_DB", cls.db_path),
            host=env("SHREDARR_HOST", cls.host),
            port=int(env("SHREDARR_PORT", str(cls.port))),
            repo=env("SHREDARR_REPO", cls.repo),
            watch_interval=float(env("SHREDARR_WATCH_INTERVAL", str(cls.watch_interval))),
            watch_confirm=float(env("SHREDARR_WATCH_CONFIRM", str(cls.watch_confirm))),
            read_only=env("SHREDARR_READ_ONLY", "").lower() in ("1", "true", "yes", "on"),
        )

    @property
    def config_dir(self) -> str:
        return os.path.dirname(self.config_path)

    @property
    def reset_file(self) -> str:
        return os.path.join(self.config_dir, "RESET-CODE.txt")

    @property
    def torrent_dir(self) -> str:
        """.torrent files kept so a fully deleted torrent can be added again (History → Redownload)."""
        return os.path.join(self.config_dir, "torrents")

    @property
    def watch_file(self) -> str:
        """The Plex library watch's last look at the library, so deletes while Shredarr was stopped are noticed too."""
        return os.path.join(self.config_dir, "library-files.json")
