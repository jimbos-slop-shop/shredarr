# Contributing to Shredarr

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs the right Python (3.13) and every dependency from `uv.lock`.

```sh
uv sync                    # .venv with Shredarr (editable) and the dev tools
uv run pytest -n auto      # all tests, in parallel (about 40 seconds)
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src            # type check (strict)
uv run tools/demo.py       # the UI with made-up data on http://localhost:8089 (password: demo)
```

CI runs the same lint, type check and tests on every push and pull request. Images are only built and published
after they pass.

## How Shredarr is put together

```
src/shredarr/
  __main__.py      command line: `shredarr` (serve) and `shredarr set-password`
  settings.py      process settings from environment variables (SHREDARR_CONFIG, SHREDARR_DB, SHREDARR_PORT, ...)
  config.py        config.json: everything set up in the web UI, with defaults (a read-only Config view + ConfigStore)
  db.py            state.db: connections and versioned schema migrations
  repositories.py  one class per table: the cleanup queue, per-torrent rules, History, sessions, the activity log
  files.py         files on disk: videos, RAR volumes, inodes, path mappings
  text.py          how things are written for people: sizes, durations, episode ranges, rules
  app.py           everything wired together, plus the background workers
  clients/         the apps Shredarr talks to; they raise ServiceError and nothing else
    servarr.py       Sonarr, Radarr, Prowlarr
    seerr.py, jellyfin.py, plex.py
    torrents/        qBittorrent, Transmission, Deluge behind one TorrentClient interface
  services/        what Shredarr does
    deletes.py       media deleted in Jellyfin (webhook) or Plex (noticed on disk) → Sonarr/Radarr/Seerr, queue torrents
    cleanup.py       the sweep: delete queued torrents once they're out of the library and their rule is met
    library.py       which torrent each library file came from (inodes, Sonarr/Radarr import history), cross-seeds
    rules.py         seeding rules
    delete_now.py, orphans.py, removal.py   Delete now and the No torrent tab
    restore.py       Undo; redownload.py: History → Redownload
    watcher.py       the Plex library watch
    media_server.py  rescans, for Jellyfin and Plex behind one interface
    auth.py, notifications.py, health.py, dashboard.py, settings_editor.py, webhook_setup.py, ...
  web/             FastAPI: sign-in middleware, routes, pages
  static/          the web UI (plain HTML/CSS/JS, no build step)
```

A Jellyfin delete, end to end: `web/app.py` puts the webhook's event on a queue → the event worker in `app.py` hands
it to `MediaDeletes.handle` (`services/deletes.py`), which updates Sonarr/Radarr/Seerr and puts the torrents on the
cleanup queue (`services/queue.py`) → `Cleaner.sweep` (`services/cleanup.py`) runs right after, and every few minutes,
and deletes each torrent once `Library.in_library` is 0 and `rules.evaluate` says its rule is met.

Some conventions:

- **Clients only raise `ServiceError`** (`clients/base.py`), so services never deal with httpx, requests or library
  exceptions. `ServiceHTTPError` carries the status code, `ServiceUnreachable` means no answer at all.
- **Library clients where they fit, thin clients where they don't.** qBittorrent, Transmission and Plex use their
  maintained client libraries. Sonarr/Radarr/Prowlarr, Seerr, Jellyfin and Deluge use small httpx clients: the
  libraries for those either miss endpoints Shredarr needs (pyarr has no filtered or per-item history, `/parse` or
  bulk episode-file deletes), are built for something else (the Jellyfin client is for players), or pull in a second
  HTTP stack (deluge-web-client).
- **The activity log is part of the UI.** Its lines are what users read, so they're written for people and tested.
  Use `activity.info(...)` for what happened and `activity.error(...)` for problems (those also notify).
- **config.json is read again when it changes**, so settings saved in the UI apply right away. Change it with
  `ConfigStore.update(...)`, never by editing what `load()` returns.
- **Database changes are migrations.** Add a function to `MIGRATIONS` in `db.py`; never edit an existing one.
- **Long jobs started from the UI** (Delete now, Undo, Redownload) run in the background with `spawn(...)`; the
  request returns right away and the UI follows along in the activity log.

## Tests

`tests/` runs Shredarr as a real process against fake apps (`tests/fakes/`: Sonarr, Radarr, Prowlarr, qBittorrent,
Transmission, Deluge, Jellyfin, Plex, Seerr, ntfy) that share a temporary disk with real files and hardlinks. The lab
(`tests/fakes/lab.py`) sets things up the way the real apps would: library files hardlinked to downloads,
Sonarr/Radarr history with download ids, season packs, upgrades, cross-seeds, RAR releases.

Tests only use Shredarr's public surface: the web API, the webhook, config.json, state.db and the command line. So
they describe what Shredarr does, not how, and a refactor shouldn't need to change them.

Some expected output (activity log lines, Delete now plans, API responses) is kept in `tests/golden/`. Those files
were first recorded from Shredarr 1.2.1, before the code was reorganized into this package, and the package
reproduces them. When you change behavior on purpose:

```sh
uv run pytest --update-goldens -k the_test
git diff tests/golden    # review every changed line: this is the visible change in behavior
```

`tests/unit/` has plain unit tests for things the lab can't reach end to end (Discord and Pushover formats, the daily
summary, update checks, retries). `tests/fixtures/state-1.2.1.db` is a database made by Shredarr 1.2.1, to keep
upgrades working.

## Trying a change on real data

Run the branch's image as a second container with `SHREDARR_READ_ONLY=1`, a copy of the real config folder, and
`/data` mounted read-only. Read-only mode is enforced in the clients: anything that would change another app (any
non-GET request, torrent client removes and adds, Plex scans, RAR unpacking) raises `ReadOnly` instead, and no
notifications are sent. `tests/test_read_only.py` checks that nothing gets through.

## Releasing

1. Set the version in `pyproject.toml` (the app reads it from there) and run `uv lock`.
2. Commit, then tag: `git tag v2.1.0 && git push --tags`.
3. CI tests it, builds the image for amd64 and arm64, and publishes `ghcr.io/jschaef80/shredarr:2.1.0` and `:latest`.
4. Write the release notes on GitHub.
