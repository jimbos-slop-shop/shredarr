# Changelog

Notable changes per release. Releases before 2.0.0 are in the [git history](https://github.com/jimbos-slop-shop/shredarr/commits/main).

## [Unreleased]

## [2.3.2] - 2026-10-17

### Changed

- The project moved to [jimbos-slop-shop/shredarr](https://github.com/jimbos-slop-shop/shredarr). The Docker image is
  now `ghcr.io/jimbos-slop-shop/shredarr`: change the `image:` line in your compose file, because the old
  `ghcr.io/jschaef80/shredarr` gets no new versions.

### Fixed

- The update check looks at the new repo. Before, GitHub answered the old address with a redirect, the check failed,
  and no "new version" notice was shown.

## [2.3.1] - 2026-10-16

Review fixes from [#7](https://github.com/jimbos-slop-shop/shredarr/pull/7).

### Fixed

- The cleanup sweep no longer fails when Sonarr or Radarr is down during the low disk space pass. Torrents it can't check wait for the next pass.
- A delete that worked is no longer logged as failed when the app answers with an empty JSON reply.
- Plex calls no longer fail at random when two things use the Plex connection at the same time.
- Settings save on shares that don't allow changing file permissions.
- Saved `.torrent` files cut off mid-write are removed after an hour, and Delete all removes them too.

### Security

- Wrong passwords also count per connection (50 in 15 minutes), so a device on your network can't dodge the limit by making up `X-Forwarded-For` addresses.

### Changed

- The "files vanished" notification for Plex is sent once per outage instead of every time the count of missing files changes.
- The Docker image runs on Python 3.14 (it was 3.13).
- Code tidy-ups with no change in behavior.

## [2.3.0] - 2026-10-16

Bug fixes, structure, performance and security work ([#6](https://github.com/jimbos-slop-shop/shredarr/pull/6)), including:

- The Jellyfin API key is sent in the Authorization header, so Jellyfin 12 accepts it ([#4](https://github.com/jimbos-slop-shop/shredarr/issues/4)).
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

[Unreleased]: https://github.com/jimbos-slop-shop/shredarr/compare/v2.3.2...HEAD
[2.3.2]: https://github.com/jimbos-slop-shop/shredarr/compare/v2.3.1...v2.3.2
[2.3.1]: https://github.com/jimbos-slop-shop/shredarr/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/jimbos-slop-shop/shredarr/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/jimbos-slop-shop/shredarr/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/jimbos-slop-shop/shredarr/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/jimbos-slop-shop/shredarr/compare/v1.2.1...v2.0.0
