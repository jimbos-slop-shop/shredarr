"""Command line: `shredarr` runs the server; `shredarr set-password 'new password'` resets the password."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import Shredarr
from .settings import Settings
from .web.app import create_app


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="shredarr", description="Delete it in Jellyfin or Plex, and it's gone everywhere.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the web app and the cleanup (the default)")
    serve.add_argument("--host", default=settings.host)
    serve.add_argument("--port", type=int, default=settings.port)
    pw = sub.add_parser("set-password", help="set the sign-in password (if you forgot it)")
    pw.add_argument("password")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    for noisy in ("httpx", "httpcore", "urllib3", "qbittorrentapi", "transmission_rpc", "plexapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    core = Shredarr(settings)
    if args.command == "set-password":
        core.auth.set_password(args.password)
        print("password set")
        return 0
    host = getattr(args, "host", settings.host)
    port = getattr(args, "port", settings.port)
    uvicorn.run(create_app(core), host=host, port=port, proxy_headers=False, access_log=False, log_level="warning",
                server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
