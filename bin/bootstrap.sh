#!/usr/bin/env bash
# Set this up from scratch on a new machine.
#
#   git clone <this repo> ~/workspace/personal/garmin-hevy-sync
#   cd ~/workspace/personal/garmin-hevy-sync
#   ./bin/bootstrap.sh
#
# Idempotent: safe to re-run after a pull.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

step() { printf '\n== %s\n' "$1"; }

step "Python environment"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Get it from https://docs.astral.sh/uv/ then re-run." >&2
  exit 1
fi
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"

step "Credentials"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from the template."
  echo
  echo "Fill in these three values, then re-run this script:"
  echo "  HEVY_API_KEY    https://hevy.com/settings?developer  (needs Hevy Pro)"
  echo "  GARMIN_EMAIL    your Garmin Connect login"
  echo "  GARMIN_PASSWORD your Garmin Connect password"
  exit 0
fi
chmod 600 .env
# shellcheck disable=SC1091
set -a; . ./.env; set +a
for var in HEVY_API_KEY GARMIN_EMAIL GARMIN_PASSWORD; do
  if [ -z "${!var:-}" ]; then
    echo "$var is empty in .env. Fill it in and re-run." >&2
    exit 1
  fi
done
echo "All three credentials present."

step "Tests"
.venv/bin/pytest -q

step "hevy2garmin settings"
# These live in ~/.hevy2garmin/config.json, outside the repo, so the choices
# encoded in config/profile.json have to be reapplied on every new machine.
.venv/bin/python - <<'PY'
import json, os
from pathlib import Path
from hevy2garmin import config as c

profile = json.loads(Path("config/profile.json").read_text())
profile.pop("_comment", None)

cfg = c.load_config()
cfg["hevy_api_key"] = os.environ["HEVY_API_KEY"]
cfg["garmin_email"] = os.environ["GARMIN_EMAIL"]
cfg["garmin_token_dir"] = "~/.garminconnect"
for key, value in profile.items():
    if isinstance(value, dict) and isinstance(cfg.get(key), dict):
        cfg[key].update(value)
    else:
        cfg[key] = value
c.save_config(cfg)
print(f"  merge_watch_strategy={cfg['merge_watch_strategy']}  profile={cfg['user_profile']}")
PY
chmod 700 ~/.hevy2garmin
chmod 600 ~/.hevy2garmin/config.json

step "Garmin sign-in"
if [ -d "$HOME/.garminconnect" ] && [ -n "$(ls -A "$HOME/.garminconnect" 2>/dev/null)" ]; then
  echo "Token store already present at ~/.garminconnect; skipping sign-in."
else
  echo "Signing in to Garmin. If the account has 2FA you will be asked for the"
  echo "code that Garmin emails you."
  .venv/bin/gh-sync login
fi

step "Connectivity check"
.venv/bin/gh-sync doctor

step "Background timer"
bash bin/install-timer.sh

printf '\nSetup complete. The sync runs every 30 minutes.\n'
printf 'Check it with: systemctl --user list-timers garmin-hevy-sync.timer\n'
