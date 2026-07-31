# garmin-hevy-sync

Two-way sync between a Garmin Venu 4 and Hevy, running unattended on this
machine. Garmin and Hevy have no official integration: Hevy has said Garmin
rejected its API request, so everything here rides on Hevy's public API plus a
community reverse-engineering of Garmin Connect.

## What it does

Four flows, run in order every 30 minutes:

| Flow | Direction | Implementation |
|------|-----------|----------------|
| A | Hevy workouts to Garmin activities | [`hevy2garmin`](https://github.com/drkostas/hevy2garmin) `sync` |
| C | Hevy routines to Garmin planned workouts | `hevy2garmin sync-routines` |
| B | Garmin watch strength sessions to Hevy workouts | this repo (`gh_sync`) |
| D | Garmin weigh-ins to Hevy body measurements | this repo (`gh_sync`) |

Flow A is the important one for the usual pattern of starting a Strength
activity on the watch for heart rate while logging the actual sets in Hevy on
your phone. It finds the watch-recorded activity and merges the Hevy sets,
reps and weights into it, so Garmin keeps real heart rate and calories instead
of getting a duplicate empty activity.

Flow B is the fallback for sessions recorded only on the watch.

## Flow order is load-bearing

A runs before B. By the time B looks at Garmin, anything that originated in
Hevy already has a Hevy counterpart inside the overlap window and is skipped.
Running B first would import a watch session into Hevy that A was about to
enrich, producing a duplicate on both sides.

## Merge strategy

When a Hevy workout matches a session your watch recorded, hevy2garmin offers
three ways to combine them. This setup uses `merge`, set in
`config/profile.json`:

| Strategy | Result |
|----------|--------|
| `merge` (in use) | Pushes sets, reps and weights into the watch activity and keeps every native metric: real heart rate, training effect, body battery. Garmin will not render exercise *names* on a device-recorded activity, so they show as "Unknown" even though the structured data is there. Nothing is deleted. |
| `replace` (upstream default) | Uploads a fresh activity with proper exercise names, then deletes the watch recording. Heart rate is carried over, but the native training effect and body battery linkage are lost. |
| `describe` | Leaves the watch activity untouched and writes the exercise list into its description. No structured sets reach Garmin. |

`merge` was chosen because the reason to wear the watch during lifting is the
heart rate and training load, and `replace` throws exactly that away.

## Loop prevention

Bidirectional sync without guards ping-pongs forever. Four defences:

1. **Pairing check.** Flow B reads hevy2garmin's ledger
   (`~/.hevy2garmin/sync.db`, table `synced_workouts`) and skips any Garmin
   activity already paired with a Hevy workout. This is the load-bearing one:
   hevy2garmin matches within 30 minutes *but also falls back to the same
   calendar day*, so a session logged into Hevy hours after the watch recorded
   it still merges correctly. The time-based check below would miss that
   pairing and import the activity a second time.
2. **Overlap check.** Flow B skips any Garmin activity starting within
   `GH_OVERLAP_MINUTES` (default 45) of an existing Hevy workout. Covers the
   window before flow A has written its ledger entry.
3. **Cross-marking.** When flow B creates a Hevy workout it immediately runs
   `hevy2garmin mark-synced <hevy_id> --garmin-id <activity_id>`, writing into
   hevy2garmin's own ledger so flow A never pushes it back.
4. **Local ledger.** `data/state.db` records every Garmin activity that flow B
   has imported or deliberately skipped. Only `failed` rows are retried.

The layering is deliberate: 1 and 2 catch different failure modes, and either
alone leaves a hole.

## Setup on a new machine

```
git clone git@github.com:danieltyukov/garmin-hevy-sync.git ~/workspace/personal/garmin-hevy-sync
cd ~/workspace/personal/garmin-hevy-sync
./bin/bootstrap.sh
```

The first run creates `.env` from the template and stops so you can fill in the
three secrets. Run it again and it installs the venv, runs the tests, writes
the hevy2garmin settings from `config/profile.json`, signs in to Garmin
(prompting for the 2FA code), verifies connectivity and installs the timer.
It is idempotent, so re-running after a `git pull` is safe.

Requires [uv](https://docs.astral.sh/uv/) and systemd.

Nothing secret is in the repo. `.env`, `data/` and `logs/` are gitignored, and
the Garmin token store lives in `~/.garminconnect`. A new machine therefore
needs the two credentials and one interactive Garmin sign-in; everything else
is reproduced from the repo.

The Hevy API key comes from <https://hevy.com/settings?developer> and needs an
active Hevy Pro subscription. Without it every flow fails at the first request.

Garmin credentials are used once. `garth` exchanges them for OAuth tokens
cached in `~/.garminconnect`, which then refresh themselves indefinitely.

### Two-factor authentication

Keep 2FA on. `gh-sync login` prompts for the emailed security code, and the
resulting tokens refresh silently from then on, so it is a single interaction
rather than an ongoing tax. Disabling 2FA would weaken the account permanently
to save that one prompt.

`hevy2garmin` reads the same `~/.garminconnect` store through its `garmin_auth`
dependency, so one `gh-sync login` authenticates all four flows.

The unattended path deliberately refuses to attempt a fresh login: under
systemd there is nobody to type a code, and an interactive prompt would hang
the unit until its timeout. If the token store ever expires, the sync fails
fast telling you to run `gh-sync login` again.

Garmin rate-limits its login endpoints and answers repeated attempts with a
429. If a sign-in fails, wait a few minutes rather than retrying immediately.

## Commands

```
gh-sync doctor              verify both credentials, list devices and recent activities
gh-sync sync                run all four flows
gh-sync sync --dry-run      report what would happen, write nothing
gh-sync sync --flows b d    run only selected flows
gh-sync status              show the ledger and the last run summary
gh-sync unmapped            Garmin exercises with no confident Hevy match
gh-sync push-to-watch       force planned workouts onto the Venu 4 now
```

## Exercise mapping

Garmin names lifts as SCREAMING_SNAKE constants (`BENCH_PRESS` /
`BARBELL_BENCH_PRESS`); Hevy uses titles with equipment in parentheses
("Bench Press (Barbell)"). Neither publishes a crosswalk, so `exercise_map.py`
normalises both into token sets and scores them with Jaccard similarity plus an
equipment-agreement bonus. The bonus is what stops a barbell bench press
binding to the dumbbell template, which scores identically on tokens alone.

Results cache to `data/exercise_map.json`. To correct a bad match, move the
entry into `overrides`, which always wins:

```json
{
  "overrides": {
    "BENCH_PRESS/BARBELL_BENCH_PRESS": "the-hevy-template-uuid"
  }
}
```

Run `gh-sync unmapped` to see what scored below the threshold. Unmapped
exercises are never silently dropped: they are named in the Hevy workout
description so the gap is visible in the app.

## Operations

```
systemctl --user list-timers garmin-hevy-sync.timer
systemctl --user status garmin-hevy-sync.service
journalctl --user -u garmin-hevy-sync.service -n 100
tail -f logs/sync.log
```

The timer uses `Persistent=true`, so a run missed while the machine was off
fires on the next boot. `RandomizedDelaySec=5min` keeps this machine off the
same instant as every other scheduled job hitting Garmin.

## Not built: Hevy webhooks

Hevy's developer settings page also offers a webhook that POSTs
`{"workoutId": "..."}` to a URL of your choosing whenever you save a workout,
expecting a 200 within 5 seconds. That would make flow A fire the moment you
rack the last set instead of within 30 minutes.

It is not wired up, because it needs a publicly reachable HTTPS endpoint on
this machine: a Cloudflare named tunnel (there is already a domain available),
a small always-on listener, and a shared secret in the authorization header.
That is a permanently exposed inbound service in exchange for shaving at most
30 minutes off a gym session's sync. The timer covers the requirement without
opening a port. Worth revisiting if the delay ever actually bites.

## Limitations

- Hevy Pro is required. The API is subscription-gated.
- Garmin has no public consumer API. `garth` tracks the mobile SSO flow, and a
  Garmin-side change can break login until the library is updated.
- Flow B cannot recover heart rate into Hevy: the Hevy API accepts sets, reps,
  weight, distance and duration, but has no field for heart rate.
- Supersets recorded on the watch import as separate consecutive exercises
  rather than a linked Hevy superset. Order is preserved.
- Deleting a workout on one side does not delete its counterpart.
- The Venu 4 often auto-detects the movement but records `repetitionCount` as 0
  with no weight. Flow B keeps those sets and puts the recorded set durations
  in the exercise note, then flags the workout description with "Needs reps and
  weight filling in" rather than importing silent blanks.
- hevy2garmin logs a non-fatal `'>' not supported between instances of
  'NoneType' and 'NoneType'` while building the activity description for a
  workout whose sets have no reps or weight. The sync itself still succeeds.

## Tests

```
.venv/bin/pytest
```

Covers tokenisation, match scoring, set grouping, unit conversion, overlap
detection and the ledger. Everything network-facing is isolated behind
`hevy.py` and `garmin_client.py` so the conversion logic tests offline.
