# Changelog

Notable changes per release. Releases before 2.0.0 are in the [git history](https://github.com/jschaef80/shredarr/commits/main).

## [Unreleased]

## [2.3.0] - 2026-10-16

Bug fixes, structure, performance and security work ([#6](https://github.com/jschaef80/shredarr/pull/6)), including:

- The Jellyfin API key is sent in the Authorization header, so Jellyfin 12 accepts it ([#4](https://github.com/jschaef80/shredarr/issues/4)).
- Proxied requests from the same machine need the password. Jellyfin webhook calls are refused when no token is set.
- Faked `X-Forwarded-For` headers no longer get around the wrong-password limit.
- Config, database and saved `.torrent` files are written safely.
- Undo, Redownload and leftover batches report failures instead of hiding them.
- The event worker and sweep loop keep running after errors.
- A notification when lots of Plex library files vanish at once.

## [2.2.0] - 2026-10-16

- Settings has a menu on the left (a row of tabs on phones) and opens one section at a time.
- Read-only mode (`SHREDARR_READ_ONLY=1`) for a test copy: it never changes another app and sends no notifications.

## [2.1.0] - 2026-10-16

- Settings → Saved .torrent files: keep them or not, for how long (1 year by default), and Delete all now.

## [2.0.0] - 2026-10-16

- Rebuilt as a Python package with the same behavior, managed with uv, with a test suite against fake apps and CI
  that publishes only tested images.
- Fixes: Undo of RAR releases with 10+ parts, the log text for Never rules, the message for a wrong qBittorrent
  password.

[Unreleased]: https://github.com/jschaef80/shredarr/compare/v2.3.0...HEAD
[2.3.0]: https://github.com/jschaef80/shredarr/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/jschaef80/shredarr/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/jschaef80/shredarr/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/jschaef80/shredarr/compare/v1.2.1...v2.0.0
