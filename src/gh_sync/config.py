"""Where things live, and the settings read from them.

Everything this tool owns sits in one per-user folder (its "home"), so it can
be found, backed up or mounted into a container in one piece:

    Linux    $XDG_CONFIG_HOME/garmin-hevy-sync   (~/.config/garmin-hevy-sync)
    macOS    ~/Library/Application Support/garmin-hevy-sync
    Windows  %APPDATA%\\garmin-hevy-sync

``GH_SYNC_HOME`` (or the ``--home`` flag) overrides it. Two things live outside
because other software owns them: the Garmin token store (``~/.garminconnect``,
shared with hevy2garmin, overridable with ``GARMINTOKENS``) and hevy2garmin's
own config and ledger in ``~/.hevy2garmin``.

Credentials come from the environment, seeded by ``config.env`` in the home.
Variables already set in the environment win, which is what Docker and CI use.
The Garmin password is deliberately never written anywhere: sign-in happens
once, interactively, and the refreshable tokens carry every run after that.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "garmin-hevy-sync"
HOME_ENV = "GH_SYNC_HOME"

logger = logging.getLogger("gh_sync.config")


def default_home() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def home_is_overridden() -> bool:
    return bool(os.environ.get(HOME_ENV))


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config_env(self) -> Path:
        return self.home / "config.env"

    @property
    def profile(self) -> Path:
        return self.home / "profile.json"

    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    @property
    def exercise_map(self) -> Path:
        return self.home / "exercise_map.json"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "sync.log"

    @property
    def lock_file(self) -> Path:
        return self.home / "sync.lock"


def paths() -> Paths:
    """Resolved on every call, so ``--home`` and test overrides take effect."""
    override = os.environ.get(HOME_ENV)
    return Paths(Path(override).expanduser() if override else default_home())


def _private_dir(path: Path) -> None:
    # Holds an API key and body stats; nobody else on the machine needs it.
    # Created 0700 rather than tightened afterwards, so there is no window in
    # which it is readable by others.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def write_private(path: Path, text: str) -> None:
    """Write a file that is owner-only from the moment it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name == "posix":
        path.chmod(0o600)  # in case it already existed with looser permissions


def ensure_home() -> Paths:
    p = paths()
    _private_dir(p.home)
    p.log_dir.mkdir(parents=True, exist_ok=True)
    return p


def garmin_token_dir() -> Path:
    return Path(os.environ.get("GARMINTOKENS") or "~/.garminconnect").expanduser()


def hevy2garmin_home() -> Path:
    # hevy2garmin hardcodes this location relative to the user's home directory.
    return Path("~/.hevy2garmin").expanduser()


def hevy2garmin_db() -> Path:
    """hevy2garmin's own ledger. Flow B consults it so an activity that flow A
    has already paired with a Hevy workout is never re-imported."""
    return hevy2garmin_home() / "sync.db"


def hevy2garmin_command() -> list[str] | None:
    """How to invoke the hevy2garmin CLI, or None if it is not installed.

    Runs it through the current interpreter instead of looking for a console
    script on PATH. Schedulers start this process without activating any
    virtualenv, so PATH lookups miss the sibling script, and on Windows a
    windowless ``pythonw`` parent keeps its child windowless too.
    """
    if importlib.util.find_spec("hevy2garmin") is None:
        return None
    return [sys.executable, "-m", "hevy2garmin.cli"]


def running_in_container() -> bool:
    return os.environ.get("GH_SYNC_CONTAINER") == "1" or Path("/.dockerenv").exists()


# ----------------------------------------------------------------- env files


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | None = None) -> None:
    """Populate os.environ from a KEY=VALUE file. Existing variables win."""
    for key, value in read_env_file(path or paths().config_env).items():
        if key not in os.environ:
            os.environ[key] = value


def update_env_file(path: Path, updates: dict[str, str | None]) -> None:
    """Set or remove keys in an env file, keeping its comments and order.

    A value of None removes the key. New keys are appended. The file is
    written owner-only because it holds the Hevy API key.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            continue
        out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")
    write_private(path, "\n".join(out) + "\n")


CONFIG_TEMPLATE = """\
# garmin-hevy-sync configuration. Written by `garmin-hevy-sync setup`.
# Environment variables with the same names take precedence over this file.

# Hevy API key from https://hevy.com/settings?developer (requires Hevy Pro)
HEVY_API_KEY=

# Garmin Connect login email. The password is never stored: sign in with
# `garmin-hevy-sync login` and the cached tokens refresh themselves.
GARMIN_EMAIL=

# Optional tuning (defaults shown)
# How many days back flow B looks for watch-recorded strength sessions
#GH_LOOKBACK_DAYS=14
# How many days back flow D looks for weigh-ins. Much longer than flow B on
# purpose: a weigh-in that falls out of the window is never picked up again
#GH_BODY_LOOKBACK_DAYS=365
# A Garmin activity starting this close to a Hevy workout is the same session
#GH_OVERLAP_MINUTES=45
# Flow B waits this long after a watch session ends before importing it, so a
# workout you save in Hevy after the gym, and flow A's pairing of it, land first
#GH_IMPORT_DELAY_MINUTES=180
# Minimum similarity (0-1) for a Garmin exercise to bind to a Hevy template
#GH_MATCH_THRESHOLD=0.55
# Mark workouts that flow B creates in Hevy as private
#GH_IMPORT_PRIVATE=false
# Also send failure notifications to this URL (an ntfy.sh topic works)
#GH_NOTIFY_URL=
"""


# ------------------------------------------------------------------ settings


class ConfigError(RuntimeError):
    """A missing or malformed setting, with a message meant for the user."""


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    hevy_api_key: str
    garmin_email: str = ""
    # Only ever read from the environment (Docker secrets, a CI login); never
    # written by this tool. Used solely by `login`.
    garmin_password: str = ""
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
    # Flow B leaves a watch session alone until this long after it ended. The
    # watch usually syncs to Garmin before the lifter has saved the workout in
    # Hevy; importing immediately would race that save and duplicate it. It
    # also has to outlast hevy2garmin's grace period (120 minutes by default)
    # plus one sync interval, because flow A only pairs a Hevy workout with
    # the watch activity once that grace period is over.
    import_delay_minutes: int = 180
    # Minimum similarity (0-1) for a Garmin exercise name to bind to a Hevy
    # exercise template. Below this the exercise is reported as unmapped.
    match_threshold: float = 0.55
    import_private: bool = False
    notify_url: str = ""
    dry_run: bool = False

    @classmethod
    def load(cls, *, dry_run: bool = False, require_hevy: bool = True) -> Settings:
        load_env()
        api_key = os.environ.get("HEVY_API_KEY", "").strip()
        if require_hevy and not api_key:
            raise ConfigError(
                f"No Hevy API key configured. Run `garmin-hevy-sync setup`, or set "
                f"HEVY_API_KEY in {paths().config_env}."
            )
        return cls(
            hevy_api_key=api_key,
            garmin_email=os.environ.get("GARMIN_EMAIL", "").strip(),
            garmin_password=os.environ.get("GARMIN_PASSWORD", ""),
            lookback_days=_int("GH_LOOKBACK_DAYS", 14, minimum=1),
            body_lookback_days=_int("GH_BODY_LOOKBACK_DAYS", 365, minimum=1),
            overlap_minutes=_int("GH_OVERLAP_MINUTES", 45),
            import_delay_minutes=_int("GH_IMPORT_DELAY_MINUTES", 180),
            match_threshold=_float("GH_MATCH_THRESHOLD", 0.55, 0.0, 1.0),
            import_private=_bool("GH_IMPORT_PRIVATE", False),
            notify_url=os.environ.get("GH_NOTIFY_URL", "").strip(),
            dry_run=dry_run,
        )


_INTERVAL = re.compile(r"^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?\s*$", re.I)


def parse_interval(text: str) -> int:
    """'30m', '30', '1h', '2 hours' -> minutes."""
    match = _INTERVAL.match(str(text))
    if not match:
        raise ConfigError(f"Cannot read {text!r} as an interval. Use e.g. 30m or 1h.")
    value = int(match.group(1))
    unit = (match.group(2) or "m").lower()
    minutes = value * 60 if unit.startswith("h") else value
    if minutes <= 0:
        raise ConfigError("The interval must be positive.")
    return minutes


# ----------------------------------------------------------- legacy migration

# A source checkout of 0.1 kept everything in the repository: .env at the root,
# config/profile.json and data/. Only meaningful when running from a checkout;
# in an installed wheel this resolves inside site-packages and finds nothing.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


def scrub_hevy2garmin_password() -> Path | None:
    """Remove a Garmin password stored in ~/.hevy2garmin/config.json.

    0.1's bootstrap ran hevy2garmin's load_config() with GARMIN_PASSWORD in the
    environment and saved the result, which wrote the password to that file.
    Returns the file's path if a password was removed.
    """
    target = hevy2garmin_home() / "config.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.pop("garmin_password", None):
        return None
    write_private(target, json.dumps(data, indent=2) + "\n")
    return target


def migrate_legacy_layout(p: Paths, checkout_root: Path | None = None) -> list[str]:
    """Copy a 0.1 checkout's config and state into the home folder, once.

    Copies rather than moves, so the old files stay as a backup, except for
    the Garmin password: nothing needs it any more, so it is removed from the
    old ``.env`` and from hevy2garmin's config instead of being carried over.
    """
    checkout_root = checkout_root or _CHECKOUT_ROOT
    legacy_env = checkout_root / ".env"
    if p.config_env.exists() or not legacy_env.is_file():
        return []
    _private_dir(p.home)
    done: list[str] = []

    values = read_env_file(legacy_env)
    had_password = bool(values.pop("GARMIN_PASSWORD", None))
    write_private(p.config_env, CONFIG_TEMPLATE)
    update_env_file(p.config_env, dict(values))
    done.append(f"{legacy_env} -> {p.config_env}")
    if had_password:
        update_env_file(legacy_env, {"GARMIN_PASSWORD": None})
        done.append(f"removed GARMIN_PASSWORD from {legacy_env}; it is no longer needed")
    scrubbed = scrub_hevy2garmin_password()
    if scrubbed:
        done.append(f"removed the stored Garmin password from {scrubbed}")

    for source, target in (
        (checkout_root / "config" / "profile.json", p.profile),
        (checkout_root / "data" / "state.db", p.state_db),
        (checkout_root / "data" / "exercise_map.json", p.exercise_map),
    ):
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            done.append(f"{source} -> {target}")
    return done
