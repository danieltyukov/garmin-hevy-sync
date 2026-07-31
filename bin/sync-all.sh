#!/usr/bin/env bash
# Entry point for the systemd timer. Runs all four flows and exits non-zero if
# any of them failed, so `systemctl --user status` reflects reality.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO/.venv/bin/gh-sync" sync "$@"
