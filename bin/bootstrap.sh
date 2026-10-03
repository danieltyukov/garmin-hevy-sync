#!/usr/bin/env bash
# Set up a development checkout: create the virtualenv, run the tests, then
# run the interactive setup from source.
#
#   git clone https://github.com/danieltyukov/garmin-hevy-sync.git
#   cd garmin-hevy-sync
#   ./bin/bootstrap.sh
#
# Most people do not need a checkout at all; see the README for the one-line
# installer. Idempotent: safe to re-run after a pull.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Get it from https://docs.astral.sh/uv/ then re-run." >&2
  exit 1
fi

uv sync
uv run pytest -q
exec uv run garmin-hevy-sync setup "$@"
