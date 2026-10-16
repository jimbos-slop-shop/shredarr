# AGENTS.md

Rules for coding agents working on Shredarr. [CONTRIBUTING.md](CONTRIBUTING.md) has the full detail; read it before
changing code. Where this file and the code disagree, the code wins: say so instead of guessing.

## Commands

```sh
uv sync                                  # set up .venv with the dev tools
uv run pytest -n auto                    # all tests, in parallel
uv run ruff check . && uv run ruff format --check .
uv run mypy src                          # strict
```

CI (`.github/workflows/ci.yml`) runs exactly these. Run all four before you call a change done.

## Where things live

- `src/shredarr/clients/`: one client per app (Sonarr/Radarr/Prowlarr, Seerr, Jellyfin, Plex, torrent clients).
- `src/shredarr/services/`: what Shredarr does (deletes, the cleanup sweep, rules, Undo, Redownload, notifications).
- `src/shredarr/web/`: FastAPI routes, sign-in middleware, pages. `static/` is the UI (no build step).
- `config.py` (config.json), `db.py` (state.db and migrations), `repositories.py` (one class per table).
- `files.py`, `text.py`: generic helpers. `app.py`: wiring and background workers.
- `tests/`: Shredarr runs as a real process against fake apps in `tests/fakes/`; `tests/unit/` for the rest.

## House rules

- **Clients only raise `ServiceError`** (`clients/base.py`) or its subclasses. Services never see httpx or library
  exceptions.
- **`files.py` and `text.py` stay generic.** Domain rules (what counts as a video, how a rule reads) go in `services/`.
- **Database changes are a new step in `MIGRATIONS`** (`db.py`). Never edit an existing step. Make each step safe to
  run again (check before adding a column, `create ... if not exists`).
- **Activity log lines are user-facing and tested.** Write them for people. Use `activity.info(...)` and
  `activity.error(...)`.
- **Golden files change only on purpose.** Run `uv run pytest --update-goldens -k the_test`, then review every line of
  `git diff tests/golden`.
- **Change config through `ConfigStore.update(...)`**, never by editing what `load()` returns.
- **Try changes on real data with `SHREDARR_READ_ONLY=1`** on a copy of the config folder. See "Trying a change on
  real data" in CONTRIBUTING.md.
- **Tests use the public surface only** (web API, webhook, config.json, state.db, command line).

## Never commit

- Secrets: API keys, passwords, tokens, tracker passkeys.
- A real `config.json`, `state.db`, `RESET-CODE.txt` or anything from a real config folder. `.gitignore` covers these;
  don't force-add them.
