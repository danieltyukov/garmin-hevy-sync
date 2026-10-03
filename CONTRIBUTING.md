# Contributing

Thanks for helping with garmin-hevy-sync. This page covers the setup, how the code is organised, how to fix an exercise match, the rules that keep the sync from duplicating data, and what to check before opening a pull request.

## Setup

You need git and [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 or newer for you if it is missing.

```sh
git clone https://github.com/danieltyukov/garmin-hevy-sync.git
cd garmin-hevy-sync
uv sync
```

`uv sync` creates `.venv` with the package and its development tools (pytest, ruff) at the versions pinned in `uv.lock`. Run the CLI from source with `uv run garmin-hevy-sync <command>`, for example `uv run garmin-hevy-sync --version`.

To add or change a dependency, use `uv add` (or edit `pyproject.toml` and run `uv lock`) and commit `uv.lock` with the change. `hevy2garmin` is pinned below 0.13 on purpose; the comment in `pyproject.toml` explains why.

## Running tests

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
```

CI runs `uv run ruff format --check .` rather than reformatting, so run `uv run ruff format .` before you push. To run part of the suite, pass a file or a `-k` filter, for example `uv run pytest tests/test_exercise_map.py -k equipment`.

Tests live in `tests/`. They run offline with no credentials and must stay that way. All network access is in `hevy.py` and `garmin_client.py`, so logic tests pass fakes in place of those clients. Do not add a test that needs a Garmin or Hevy account, and write any files a test creates under pytest's `tmp_path`.

## Trying a change against your own accounts

```sh
GH_SYNC_HOME=/tmp/gh-sync-dev uv run garmin-hevy-sync setup
GH_SYNC_HOME=/tmp/gh-sync-dev uv run garmin-hevy-sync sync --dry-run
```

`GH_SYNC_HOME` points the tool at a separate home folder, so a test configuration does not overwrite your real one (`~/.config/garmin-hevy-sync` on Linux, `~/Library/Application Support/garmin-hevy-sync` on macOS, `%APPDATA%\garmin-hevy-sync` on Windows). Two things stay shared because other software owns them: the Garmin token store in `~/.garminconnect` (set `GARMINTOKENS` to use a different one) and hevy2garmin's configuration and ledger in `~/.hevy2garmin`.

`--dry-run` reports what each flow would do and writes nothing to Garmin or Hevy. Keep it on until you are sure a change behaves as expected: without it, a sync writes to your real accounts. Code that writes to Garmin or Hevy must respect `--dry-run`.

## Layout

All code is in `src/gh_sync/`:

- `cli.py`: the `garmin-hevy-sync` commands (`gh-sync` is an alias).
- `config.py`: the home folder and settings.
- `flows.py`: flows B (watch sessions to Hevy), D (weigh-ins to Hevy) and E (exercise name repair).
- `h2g.py`: runs the `hevy2garmin` CLI as a child process for flows A and C, and for cross-marking.
- `convert.py`: pure conversion of Garmin sets into a Hevy workout payload.
- `exercise_map.py`: matching Garmin exercises to Hevy exercises, including overrides.
- `garmin_client.py` and `hevy.py`: all network access. Nothing else talks to Garmin or Hevy.
- `state.py`: the SQLite ledger, `state.db`.
- `schedule.py`: the background schedule for systemd, cron, launchd and Windows Task Scheduler. The entries it writes run `python -m gh_sync` (`__main__.py`) rather than the console script.
- `notify.py`: failure notifications.
- `setup_wizard.py`: the interactive `setup` command.

`site/` holds the project site and the installers (`install.sh`, `install.ps1`). `docs/ARCHITECTURE.md` explains the design in more depth.

## Exercise matching

Garmin names a lift with a category and a name, both constants (`BENCH_PRESS` / `BARBELL_BENCH_PRESS`). Hevy uses titles with the equipment in parentheses ("Bench Press (Barbell)"). `exercise_map.py` turns both into sets of words and scores how well they overlap. Most bad matches come from a word that the two sides spell differently, and the fix is usually one entry in a table at the top of `exercise_map.py`:

- `SYNONYMS` maps a Garmin spelling or abbreviation onto the word Hevy uses, such as `"db": "dumbbell"` or `"flyes": "fly"`. Write the value the way `tokenize()` produces it for the Hevy title: lowercase and singular.
- `EQUIPMENT` lists equipment words. Matching equipment counts towards a match, contradicting equipment rules a template out, and equipment that only Hevy mentions costs little. A new equipment word also needs a rank in `EQUIPMENT_PRIORITY`, which decides between templates that tie when Garmin names no equipment.
- `STOPWORDS` lists words that carry no meaning on either side.

Prefer a table entry over changing the weights in `score()`. The weights were checked against Garmin's full exercise catalogue, and moving them shifts matches for exercises you did not look at.

Every mapping change needs a test in `tests/test_exercise_map.py`. Add the Hevy templates involved to `TEMPLATES` with an invented id (`T_...`) and the real Hevy title, then add a test that resolves the Garmin key to the right one. `TestEquipmentDisambiguation` shows the pattern. Run the whole file afterwards: a synonym changes how every exercise is tokenised, so a fix for one lift can move another.

### Reporting a mapping problem

If you do not want to change the code, open an issue with the "Exercise mapping problem" template. It asks for the Garmin key as `garmin-hevy-sync unmapped` prints it (or as it appears in `exercise_map.json`), the Hevy exercise it matched, and the one it should have matched.

You can fix the match on your own machine while you wait: add an entry under `overrides` in `exercise_map.json` in the tool's home folder. The key is the Garmin key and the value is the Hevy exercise template id; `resolved` entries in the same file show ids next to their titles. Overrides always win over the matcher.

This matching only applies to flow B, which imports watch sessions into Hevy. A Hevy exercise that shows up under the wrong name in Garmin Connect comes from hevy2garmin's own mapping in flow A.

## Rules that keep the sync safe

### Flow order is load-bearing

A sync runs the flows in the order A, C, B, D, E.

A runs before B. By the time B looks at Garmin, any session that started in Hevy has already been merged into its Garmin activity and paired, so B skips it. With B first, B would import a watch session into Hevy that A was about to merge, and the session would end up duplicated on both sides.

E runs last because it reads back what A wrote to Garmin.

Keep this order when you add to or change a flow.

### Loop prevention

A two-way sync without guards sends the same workout back and forth forever. Four checks stop that:

1. Pairing check: flow B skips any Garmin activity that hevy2garmin's ledger (in `~/.hevy2garmin`) already pairs with a Hevy workout.
2. Overlap check: flow B skips any Garmin activity that starts within the overlap window of an existing Hevy workout, or whose time range intersects one. This covers the time before flow A has written its ledger entry.
3. Cross-marking: when flow B creates a Hevy workout, it records the pair in hevy2garmin's ledger (`hevy2garmin mark-synced`) so flow A never pushes it back to Garmin.
4. Local ledger: `state.db` records every Garmin activity that flow B imported or deliberately skipped. Only failed rows are retried.

Each check covers a case the others miss. The pairing check exists because hevy2garmin also pairs a workout with a watch activity from the same calendar day, hours apart, which the overlap check cannot see. Do not remove or narrow one of them because another seems to cover the same case. A pull request that touches any of them needs a test for the case that check exists for.

## Fixtures and privacy

Fixtures must never contain real data. Before committing a captured Garmin or Hevy response:

- Replace activity ids, Hevy workout ids, custom exercise template ids, owner names and ids, device ids and email addresses with invented values.
- Remove GPS coordinates, body stats (weight, birth year, sex, VO2 max), API keys and anything from `~/.garminconnect`, `~/.hevy2garmin` or the tool's home folder.
- Keep only the fields the code reads, so a fixture documents the shape rather than a person.

A pull request that contains real personal data or credentials will be closed and the history rewritten.

## Commits

Commits follow the conventional commit format: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`, `ci:`, optionally with a scope such as `fix(flow-b):`. Keep the subject under 72 characters and describe the change, not the process. Do not add generated-by notices, AI or tool attribution trailers, or session links.

Formatting and linting are enforced by ruff with the settings in `pyproject.toml`. No emojis in code, comments, docs or commit messages.

## Pull request checklist

CI runs on Ubuntu, macOS and Windows with Python 3.12 and 3.13. Each job runs `ruff check`, `ruff format --check` and `pytest`, then builds the package and smoke-tests the installed CLI. All of them must pass.

- `uv run ruff check . && uv run ruff format --check . && uv run pytest` passes locally.
- New logic has a test that runs offline.
- No personal data in fixtures or the diff: no activity ids tied to a person, email addresses, API keys, tokens or body stats.
- README, docs or `CHANGELOG.md` (under Unreleased) updated if behaviour changed.
- Scheduler or installer changes tested on the operating system they affect. Say which in the pull request.

## Releases

1. Bump `__version__` in `src/gh_sync/__init__.py`. The package version is read from there.
2. In `CHANGELOG.md`, move the entries under Unreleased to a new heading for the version and date.
3. Commit as `chore: release vX.Y.Z`, tag the commit `vX.Y.Z` and push the tag.

The release workflow builds the wheel and the Docker image from the tag.

## Questions

Questions, setup help and ideas go in [GitHub Discussions](https://github.com/danieltyukov/garmin-hevy-sync/discussions). Bugs and concrete feature requests go in issues.
