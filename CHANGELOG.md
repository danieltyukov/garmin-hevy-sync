# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-03

Runs on Linux, macOS, Windows and Docker, installs with one command, and no longer needs a git clone.

### Added

- One-line installers for macOS and Linux (`install.sh`) and Windows (`install.ps1`). They install uv if it is missing, which brings its own Python, so nothing else is required.
- `garmin-hevy-sync setup`: an interactive, re-runnable setup that checks the Hevy key against Hevy, signs in to Garmin (including the emailed MFA code), asks for the calorie profile, configures hevy2garmin and turns on the background sync.
- `garmin-hevy-sync schedule install|remove|status`: background syncing through systemd (cron as a fallback) on Linux, launchd on macOS and Task Scheduler on Windows, where it runs without a console window.
- Docker image and `compose.yaml` for NAS boxes, Raspberry Pis and home servers. Release builds publish `ghcr.io/danieltyukov/garmin-hevy-sync` for amd64 and arm64.
- `sync --every 30m` keeps syncing in the foreground, for containers and process supervisors.
- Notifications when a background run fails (desktop, plus an optional ntfy-style `GH_NOTIFY_URL`), once when the problem starts and at most daily after that.
- `garmin-hevy-sync logs [-f]`, `--version`, `--home`, and `status` now shows the schedule, the last run per flow and all-time totals.
- `GH_IMPORT_DELAY_MINUTES` (default 180): flow B waits this long after a watch session ends, so a workout saved in Hevy after the gym, and flow A's pairing of it after hevy2garmin's two-hour grace period, land first instead of being duplicated.
- Flow B also treats a Hevy workout whose time range intersects the watch session as the same session, not only one that started within `GH_OVERLAP_MINUTES` of it.
- `GH_IMPORT_PRIVATE` to create flow B's Hevy workouts as private.
- `login --mfa-file PATH` replaces `bin/login_assisted.py`.
- The command is now `garmin-hevy-sync`; `gh-sync` remains as an alias.
- Website at https://danieltyukov.github.io/garmin-hevy-sync/, contributor guide, security policy, code of conduct, issue templates and CI on all three operating systems.

### Changed

- Settings, ledger, exercise map and logs moved from the repository to one per-user folder (`~/.config/garmin-hevy-sync`, `~/Library/Application Support/garmin-hevy-sync` or `%APPDATA%\garmin-hevy-sync`). A 0.1 checkout is migrated automatically on first run, and the originals are kept.
- The systemd timer is now a calendar timer, so `Persistent=true` actually catches up runs missed while the machine was asleep or off. With `OnUnitActiveSec=`, as in 0.1, it had no effect.
- Each of flows B, D and E runs in its own error boundary: one failing no longer stops the others, and a failure to read Garmin's weigh-ins is reported as a failed flow D instead of an all-zero summary.
- When several weigh-ins share a date, flow D sends the first reading of the day instead of whichever Garmin listed first.
- hevy2garmin output is logged at INFO unless a line reports a problem, so a healthy run no longer looks like a wall of warnings.
- Requires Python 3.12 or newer. Dependencies: hevy2garmin 0.12 (pinned below 0.13, because its PyPI package stops releasing after 2026-10-31), garminconnect 0.3.17. Dependencies are locked in `uv.lock`.

### Fixed

- Ledger rows were only committed at the end of a run, so a crash halfway through forgot workouts already created in Hevy and the next run could create them again. Every write is now committed immediately.
- Two syncs could run at once (a manual run during a scheduled one) and both import the same activity. A run lock now prevents that.
- The interactive login's fallback called `client.garth.dump`, which no longer exists in garminconnect 0.3.
- A background run that could not start (for example a missing API key) printed its error to nowhere. It is now logged, recorded as a failed run and notified.
- Unattended runs could still attempt a password sign-in when the token store was missing, which cannot answer an MFA prompt and earns a 429 from Garmin. They now fail fast with an instruction to run `login`.
- A network outage was reported as "sign-in needed". It is now reported as Garmin being unavailable.
- Hevy reads are retried on dropped connections, timeouts and 5xx, and honour `Retry-After` on 429. Writes are retried only when Hevy provably did not receive them, so a timeout can never create the same workout twice.
- Hevy pagination detected a rejected page size by searching the error text for "400", which any id containing those digits would also match. It now checks the status code.

### Security

- The Garmin password is no longer stored anywhere. 0.1 kept it in `.env`, and its bootstrap also wrote it into `~/.hevy2garmin/config.json` through hevy2garmin's config loader. The migration removes it from both instead of copying it, the hevy2garmin child process never receives it, and `doctor` warns if it reappears.
- The home folder is created owner-only on Linux and macOS, and `config.env`, `profile.json` and hevy2garmin's config are created with mode 600 from the start rather than tightened afterwards.

## [0.1.0] - 2026-08-14

First public version: five flows between Garmin Connect and Hevy on a systemd timer, including the fix that makes pushed exercise names render in Garmin Connect.

[Unreleased]: https://github.com/danieltyukov/garmin-hevy-sync/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/danieltyukov/garmin-hevy-sync/releases/tag/v0.2.0
[0.1.0]: https://github.com/danieltyukov/garmin-hevy-sync/commits/a0b7ede
