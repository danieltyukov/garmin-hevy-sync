## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

<!-- The tests you added. For scheduler or installer changes, the operating system you ran them on. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pytest` passes locally.
- [ ] New logic has a test that runs offline, with no credentials.
- [ ] Fixtures and the diff contain no personal data: no activity ids tied to a person, email addresses, API keys, tokens or body stats.
- [ ] README, docs or `CHANGELOG.md` (under Unreleased) updated if behaviour changed.
- [ ] Scheduler or installer changes tested on the affected operating system, named above.
- [ ] Flow order and the loop-prevention checks are unchanged, or the change is explained above.
