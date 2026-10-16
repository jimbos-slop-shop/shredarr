"""Runs the legacy single-file Shredarr (v1.2.1) the way tests/harness.py expects: on SHREDARR_PORT, with the
library watcher's timings taken from SHREDARR_WATCH_INTERVAL / SHREDARR_WATCH_CONFIRM. Kept only while the
refactor is checked against it."""

import os
import secrets
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shredarr_legacy as s  # noqa: E402

if sys.argv[1:2] == ["set-password"]:
    s.set_password(sys.argv[2])
    sys.exit(print("password set"))

s.WATCH_CONFIRM = float(os.environ.get("SHREDARR_WATCH_CONFIRM", "100"))
interval = float(os.environ.get("SHREDARR_WATCH_INTERVAL", "60"))


def watcher():
    while True:
        try:
            c = s.cfg()
            if s.media_kind(c) == "plex" and not s.configured(c):
                s.watch_pass()
        except Exception as e:
            print("library watch:", e, flush=True)
        time.sleep(interval)


s.db().close()
for fn in (s.worker, s.sweeper, s.summarizer, s.monitor, watcher):
    threading.Thread(target=fn, daemon=True).start()
if not s.cfg().get("webhook_token"):
    c0 = s.cfg()
    c0["webhook_token"] = secrets.token_hex(32)
    s.save_cfg(c0)
s.log("Shredarr started")
ThreadingHTTPServer(("0.0.0.0", int(os.environ["SHREDARR_PORT"])), s.Handler).serve_forever()
