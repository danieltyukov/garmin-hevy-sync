"""Runtime configuration: paths and credentials.

Credentials come from the environment, seeded by ``.env`` in the repo root. The
file is chmod 600 and gitignored; nothing here ever writes a secret back out.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = REPO_ROOT / "logs"
ENV_FILE = REPO_ROOT / ".env"

STATE_DB = DATA_DIR / "state.db"
# hevy2garmin's own ledger. Flow B consults it so an activity that flow A has
# already paired with a Hevy workout is never re-imported.
HEVY2GARMIN_DB = Path("~/.hevy2garmin/sync.db").expanduser()
EXERCISE_MAP_FILE = DATA_DIR / "exercise_map.json"
GARMIN_TOKENS = Path("~/.garminconnect").expanduser()


def load_env(path: Path = ENV_FILE) -> None:
    """Populate os.environ from a KEY=VALUE file. Existing vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    hevy_api_key: str
    garmin_email: str
    garmin_password: str
    # Flow B looks this many days back for watch-recorded strength sessions.
    lookback_days: int = 14
    # Flow D gets its own, much longer window. Weigh-ins are sparse and
    # irregular, so a fortnight sized for near-daily workouts silently drops
    # any weigh-in older than that instead of merely deferring it: nothing
    # ever widens the window again, so a missed entry is missed permanently.
    body_lookback_days: int = 365
    # A Garmin activity starting within this many minutes of an existing Hevy
    # workout is assumed to be the same session, so flow B leaves it alone.
    overlap_minutes: int = 45
    # Minimum similarity (0-1) for a Garmin exercise name to bind to a Hevy
    # exercise template. Below this the exercise is reported as unmapped.
    match_threshold: float = 0.55
    dry_run: bool = False

    @classmethod
    def from_env(cls, dry_run: bool = False) -> "Settings":
        load_env()
        missing = [
            name
            for name in ("HEVY_API_KEY", "GARMIN_EMAIL", "GARMIN_PASSWORD")
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(
                f"Missing credentials: {', '.join(missing)}.\n"
                f"Fill them into {ENV_FILE} (see .env.example)."
            )
        return cls(
            hevy_api_key=os.environ["HEVY_API_KEY"],
            garmin_email=os.environ["GARMIN_EMAIL"],
            garmin_password=os.environ["GARMIN_PASSWORD"],
            lookback_days=int(os.environ.get("GH_LOOKBACK_DAYS", "14")),
            body_lookback_days=int(os.environ.get("GH_BODY_LOOKBACK_DAYS", "365")),
            overlap_minutes=int(os.environ.get("GH_OVERLAP_MINUTES", "45")),
            match_threshold=float(os.environ.get("GH_MATCH_THRESHOLD", "0.55")),
            dry_run=dry_run,
        )


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def hevy2garmin_binary() -> str | None:
    """Absolute path to the hevy2garmin CLI, or None if it is not installed.

    Looking it up on PATH alone is wrong here. The systemd unit execs the venv's
    gh-sync directly rather than activating the venv, so .venv/bin never joins
    PATH and its own sibling console script appears to be missing. Resolving
    against the running interpreter finds it regardless of activation.
    """
    sibling = Path(sys.executable).parent / "hevy2garmin"
    if sibling.exists():
        return str(sibling)
    return shutil.which("hevy2garmin")
