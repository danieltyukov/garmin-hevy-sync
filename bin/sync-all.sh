#!/usr/bin/env bash
# Kept so that systemd timers installed by version 0.1 (which point here) keep
# working after a `git pull`. New installs do not use this file:
# `garmin-hevy-sync schedule install` writes units that call the tool directly.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO/.venv/bin/python" -m gh_sync sync "$@"
