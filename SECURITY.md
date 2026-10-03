# Security policy

garmin-hevy-sync keeps a Hevy API key, a Garmin email address, Garmin OAuth tokens and body stats on the user's machine, and it runs unattended on a schedule, writing workouts, planned workouts and body measurements to the user's Garmin and Hevy accounts. Problems in those areas matter more than anywhere else in the project.

The Garmin password is never stored. It is used once during sign-in to obtain the OAuth tokens.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub security advisories: open the repository's Security tab and choose "Report a vulnerability", or go directly to https://github.com/danieltyukov/garmin-hevy-sync/security/advisories/new. Include the version (`garmin-hevy-sync --version`), the operating system, how it was installed (installer script, `uv tool`, Docker or a git clone), the steps to reproduce, and what an attacker could gain. If you have a fix, a draft pull request attached to the advisory is welcome.

## Scope

In scope:

- Credential and token storage: the Garmin token store in `~/.garminconnect`, hevy2garmin's configuration and ledger in `~/.hevy2garmin`, and the tool's home folder (`~/.config/garmin-hevy-sync` on Linux, `~/Library/Application Support/garmin-hevy-sync` on macOS, `%APPDATA%\garmin-hevy-sync` on Windows) with `config.env`, `profile.json`, `state.db`, `exercise_map.json` and `logs/`. This covers file permissions, the Garmin password reaching disk, and secrets showing up in logs, in `garmin-hevy-sync doctor` output or in failure notifications.
- The installers, `site/install.sh` and `site/install.ps1`.
- The scheduler entries the tool writes: systemd units, the launchd plist, the Windows scheduled task and the crontab line.
- The Docker image.
- Anything that could send data somewhere other than Hevy, Garmin or the user's own `GH_NOTIFY_URL`.
- Writes to the wrong Garmin or Hevy account, duplicate writes, and writes that delete or overwrite data the user did not ask to change.

Out of scope:

- Vulnerabilities in Garmin Connect or Hevy themselves. Report those to Garmin or Hevy.
- Problems in hevy2garmin itself, which carries out flows A and C. Report those upstream at https://github.com/drkostas/hevy2garmin.
- Issues that require an attacker who already has the user's OS account.
- Problems in other third-party dependencies with no demonstrated impact on this project. Those are still useful to hear about, but a normal issue is fine.

## What to expect

You should get an acknowledgement within seven days. The project is maintained by one person in their spare time, so please allow some slack; if you have heard nothing after two weeks, comment on the advisory. Confirmed problems are fixed in a patch release and described in `CHANGELOG.md` and the advisory once the fix is available. Credit is given to the reporter unless they ask otherwise.

## Supported versions

Only the latest release receives fixes.
