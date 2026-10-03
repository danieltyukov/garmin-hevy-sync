<p align="center">
  <img src="site/logo.svg" alt="garmin-hevy-sync" width="340">
</p>

<p align="center">Two-way sync between a Garmin watch and Hevy, running quietly in the background.</p>

<p align="center">
  <a href="https://github.com/danieltyukov/garmin-hevy-sync/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/danieltyukov/garmin-hevy-sync/ci.yml?branch=main&label=CI" alt="CI status"></a>
  <a href="https://github.com/danieltyukov/garmin-hevy-sync/releases"><img src="https://img.shields.io/github/v/release/danieltyukov/garmin-hevy-sync" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12 or newer">
  <img src="https://img.shields.io/badge/runs%20on-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Docker-555" alt="Runs on Linux, macOS, Windows and Docker">
</p>

<p align="center">
  <a href="https://danieltyukov.github.io/garmin-hevy-sync/">Website</a> ·
  <a href="#install">Install</a> ·
  <a href="docs/ARCHITECTURE.md">How it works</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

You start a Strength activity on the watch so you get real heart rate and
training load, but you log the actual sets in [Hevy](https://www.hevyapp.com/)
on your phone, because typing reps on a watch is miserable. Without a sync you
end up with half the session in each app.

Garmin and Hevy have no official integration (Hevy has said Garmin rejected its
API request). garmin-hevy-sync connects them using Hevy's public API and a
community reverse-engineering of Garmin Connect, and keeps them in step every
30 minutes on Linux, macOS, Windows or in Docker.

## What it does

| Flow | Direction | What you get |
|------|-----------|--------------|
| A | Hevy workouts to Garmin | Your sets, reps and weights merged into the watch recording, which keeps its real heart rate and training effect |
| C | Hevy routines to Garmin | Routines appear as planned workouts you can start on the watch |
| B | Watch sessions to Hevy | Sessions recorded only on the watch show up in Hevy, exercises matched to Hevy's catalogue |
| D | Garmin weigh-ins to Hevy | Body weight, body fat and lean mass from your scale in Hevy's body measurements |
| E | Garmin to Garmin | Exercise names render instead of "Choose an Exercise", and the muscle map fills in |

Flows A and C use [hevy2garmin](https://github.com/drkostas/hevy2garmin); B, D
and E are this project. Several layers of loop prevention make sure nothing
gets copied back and forth. [How it works](docs/ARCHITECTURE.md) explains the
details.

### The exercise-name fix

Merging Hevy sets into a watch-recorded activity used to leave every set
showing as "Choose an Exercise", even though Garmin had stored a correct name
for each one. The cause was a zero `probability` (confidence) value on every
pushed set; [the write-up](docs/ARCHITECTURE.md#flow-e-making-the-exercise-names-render)
explains it. Flow E repairs it automatically.

| Before | After |
|--------|-------|
| ![Every set showing Choose an Exercise](docs/img/before-choose-an-exercise.png) | ![The same sets showing Pistol Squat and Lunge](docs/img/after-named-exercises.png) |

The muscle map comes back too:

![Garmin muscle map showing primary and secondary muscles](docs/img/muscle-map.png)

## Requirements

- **Hevy Pro.** The Hevy API is only available to subscribers.
- **A Garmin Connect account** and a watch that records Strength activities.
  Developed against a Venu 4; anything that writes exercise sets should work.
- **A computer that is on most of the time**, running Linux, macOS or Windows,
  or anything that runs Docker (a NAS, a Raspberry Pi, a home server).

Nothing else. The installer brings [uv](https://docs.astral.sh/uv/), which
brings its own Python.

## Install

**macOS and Linux**

```sh
curl -LsSf https://danieltyukov.github.io/garmin-hevy-sync/install.sh | sh
```

**Windows** (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://danieltyukov.github.io/garmin-hevy-sync/install.ps1 | iex"
```

**Docker**

```sh
git clone https://github.com/danieltyukov/garmin-hevy-sync.git
cd garmin-hevy-sync
docker compose run --rm garmin-hevy-sync setup
docker compose up -d
```

**With uv directly**

```sh
uv tool install git+https://github.com/danieltyukov/garmin-hevy-sync
garmin-hevy-sync setup
```

The installer finishes by starting `garmin-hevy-sync setup`, which walks
through four steps:

1. **Hevy.** Paste an API key from <https://hevy.com/settings?developer>. It is
   checked against Hevy straight away.
2. **Garmin.** Sign in once with your email, password and the security code
   Garmin emails you. The password is used for that sign-in only and is never
   stored; the refreshable tokens it produces carry every later run.
3. **Calorie profile.** Weight, birth year and sex for hevy2garmin's calorie
   estimate. Press Enter to keep the defaults; wrong values only mean less
   accurate calories.
4. **Background sync.** Turns on a sync every 30 minutes using your system's
   own scheduler.

It ends by offering a dry run that shows what the first sync would do without
writing anything. Setup is safe to run again at any time; it keeps what works
and only asks about what is missing.

## Everyday use

Once set up there is nothing to do. These are for checking on it:

```
garmin-hevy-sync status            last run, schedule, totals
garmin-hevy-sync doctor            check credentials, connectivity and the schedule
garmin-hevy-sync logs -f           follow the log
garmin-hevy-sync sync              sync now
garmin-hevy-sync sync --dry-run    show what a sync would do, write nothing
garmin-hevy-sync sync --flows b d  run only some flows
garmin-hevy-sync unmapped          Garmin exercises with no confident Hevy match
garmin-hevy-sync push-to-watch     send planned workouts to the watch now
garmin-hevy-sync login             sign in to Garmin again
garmin-hevy-sync schedule status   is the background sync on?
```

`gh-sync` is a shorter alias for the same command.

## Running in the background

`garmin-hevy-sync schedule install` (which setup runs for you) uses whatever
your system already has:

| System | Scheduler | Runs missed while asleep or off |
|--------|-----------|---------------------------------|
| Linux | systemd user timer (cron if there is no systemd user session) | caught up on wake (not with cron) |
| macOS | launchd agent | caught up on wake |
| Windows | Task Scheduler, runs without a console window | caught up when the PC is next on |
| Docker | the container's own loop | n/a |

Change the interval with `schedule install --every 1h`; it must divide an hour
or a day evenly (15m, 20m, 30m, 1h, 2h, ...). Turn it off with
`schedule remove`.

On Linux, setup also enables lingering where your system allows it, so the
timer keeps running while you are logged out. If it cannot, it prints the one
`sudo loginctl enable-linger` command that does.

### Where things live

Everything belongs to your user account and stays on your machine:

| What | Where |
|------|-------|
| Settings, ledger, exercise map, logs | Linux `~/.config/garmin-hevy-sync`, macOS `~/Library/Application Support/garmin-hevy-sync`, Windows `%APPDATA%\garmin-hevy-sync` (override with `GH_SYNC_HOME` or `--home`) |
| Garmin sign-in tokens | `~/.garminconnect` (shared with hevy2garmin) |
| hevy2garmin settings and ledger | `~/.hevy2garmin` |
| Docker | all of the above inside the `data` volume |

Nothing is sent anywhere except Hevy, Garmin, and the notification URL if you
set one.

### When something goes wrong

A failed background run raises a desktop notification, once when the problem
starts and then at most once a day. For a headless machine or Docker, set
`GH_NOTIFY_URL` to an [ntfy](https://ntfy.sh) topic URL (or anything that
accepts a plain-text POST) and the same message is pushed there.

## Configuration

`config.env` in the home folder holds the settings. `setup` writes the
credentials; the rest are optional. Environment variables with the same names
take precedence, which is how Docker and CI configure it.

| Variable | Default | Meaning |
|----------|---------|---------|
| `HEVY_API_KEY` | | Hevy developer API key |
| `GARMIN_EMAIL` | | Garmin Connect login, used by `login` |
| `GH_LOOKBACK_DAYS` | 14 | How far back flow B looks for watch sessions |
| `GH_BODY_LOOKBACK_DAYS` | 365 | How far back flow D looks for weigh-ins |
| `GH_OVERLAP_MINUTES` | 45 | A Garmin activity this close to a Hevy workout is treated as the same session |
| `GH_IMPORT_DELAY_MINUTES` | 180 | Flow B waits this long after a watch session ends, so a workout you save in Hevy afterwards, and flow A's pairing of it, land first |
| `GH_MATCH_THRESHOLD` | 0.55 | Minimum similarity to bind a Garmin exercise to a Hevy template |
| `GH_IMPORT_PRIVATE` | false | Mark workouts that flow B creates in Hevy as private |
| `GH_NOTIFY_URL` | | Also push failure notifications to this URL |

The two lookback windows differ on purpose. Workouts are frequent, so a
fortnight is plenty. Weigh-ins are sparse, and one that falls out of the window
is never picked up again, so flow D gets a year. When you weigh in several
times a day, the first reading of the day is the one sent to Hevy.

`profile.json` in the home folder holds the settings `setup` applies to
hevy2garmin: your calorie profile, `merge_watch_strategy` and
`sync.grace_period_minutes`.

| `merge_watch_strategy` | Result |
|------------------------|--------|
| `merge` (default here) | Pushes sets, reps and weights into the watch activity and keeps every native metric: real heart rate, training effect, body battery. |
| `replace` | Uploads a new activity with the exercise names, then deletes the watch recording. Heart rate is carried over; native training effect is lost. |
| `describe` | Leaves the watch activity alone and writes the exercise list into its description. |

`grace_period_minutes` (120) is how long hevy2garmin waits after a Hevy workout
ends before syncing it, so the watch recording has reached Garmin and gets
merged instead of duplicated. Run `garmin-hevy-sync setup` after editing
`profile.json` to apply it.

To fix an exercise that matched the wrong Hevy exercise, see
[Exercise mapping](docs/ARCHITECTURE.md#exercise-mapping).

## Updating

Run the install command again; it upgrades in place and keeps your settings.
With Docker: `git pull && docker compose up -d --build`.

### Upgrading from 0.1

Version 0.1 ran from a git clone and kept its files in the repository. In that
clone, run `git pull` and then `./bin/bootstrap.sh`. The first command run from
the updated clone copies `.env`, `config/profile.json` and `data/` into the new
home folder, leaving the originals in place as a backup. The Garmin password is
not carried over: it is removed from the old `.env` and from the copy that 0.1
left in `~/.hevy2garmin/config.json`. Setup then replaces the old systemd units with
new ones. The old timer keeps working until then, so there is no gap.

Once migrated you can keep running from the clone, or switch to the one-line
installer: it uses the same home folder, so run `garmin-hevy-sync setup` once
afterwards to point the schedule at the installed copy.

## Uninstalling

```sh
garmin-hevy-sync schedule remove
uv tool uninstall garmin-hevy-sync
```

Then delete the home folder listed above, and `~/.garminconnect` and
`~/.hevy2garmin` if nothing else uses them. With Docker:
`docker compose down -v`.

## Troubleshooting

Start with `garmin-hevy-sync doctor`. It checks both credentials and the
schedule before you debug anything else.

**"Garmin sign-in needed".** The tokens expired or were revoked. Run
`garmin-hevy-sync login` in a terminal (in Docker:
`docker compose run --rm garmin-hevy-sync login`). Background runs never try
to sign in on their own, because nobody is there to type the emailed code.

**Sign-in returns 429.** Garmin rate-limits sign-ins. Wait 15 minutes; retrying
immediately extends the block.

**Hevy fails with 401.** The API key is wrong, or Hevy Pro has lapsed.

**Exercises still show "Choose an Exercise".** Check `status` for flow E, then
run `garmin-hevy-sync sync --flows e`.

**A workout synced twice.** Look at `state.db` in the home folder (flow B) and
`~/.hevy2garmin/sync.db` (flow A's pairings), and please open an issue.

**A watch session has not appeared in Hevy yet.** Flow B waits three hours after
a session ends (`GH_IMPORT_DELAY_MINUTES`) in case you save it in Hevy yourself.

**The summary shows all zeros.** That is normal when there is nothing new.
Every flow reports a `considered` or `checked` count so "nothing to do" stays
distinguishable from "nothing found".

## Limitations

- Hevy Pro is required. The API is subscription-gated.
- This is an unofficial integration. Garmin has no public consumer API, and a
  change on Garmin's side can break sign-in until the libraries catch up.
- hevy2garmin's PyPI package, used for flows A and C, gets no new releases
  after 2026-10-31. The pinned version keeps working; see
  [Upstream dependency](docs/ARCHITECTURE.md#upstream-dependency).
- Flow B cannot carry heart rate into Hevy: the Hevy API has no field for it.
- Supersets recorded on the watch import as separate consecutive exercises
  rather than a linked Hevy superset. Order is preserved.
- Deleting a workout on one side does not delete its counterpart.
- Flow D is one-way. A weight logged only in Hevy stays there.
- Syncing is periodic, not instant. A session appears within the interval
  (30 minutes by default) plus the delays described above.
- Some watches detect the movement but record zero reps and no weight. Flow B
  keeps those sets, puts the set durations in the exercise note, and flags the
  workout with "Needs reps and weight filling in".

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers
the development setup (`uv sync`, `uv run pytest`), the layout and the
conventions. Ideas and questions go in
[Discussions](https://github.com/danieltyukov/garmin-hevy-sync/discussions).
Report security problems privately, as described in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

This project is not affiliated with, endorsed by, or supported by Garmin or
Hevy. Use it at your own risk.
