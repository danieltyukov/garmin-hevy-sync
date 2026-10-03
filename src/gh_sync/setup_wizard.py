"""`garmin-hevy-sync setup`: everything needed on a new machine, in one pass.

Idempotent. Re-running it keeps whatever already works and only asks about
what is missing or broken, so it doubles as the repair command.
"""

from __future__ import annotations

import copy
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import schedule
from .config import (
    CONFIG_TEMPLATE,
    ConfigError,
    ensure_home,
    garmin_token_dir,
    hevy2garmin_home,
    load_env,
    running_in_container,
    update_env_file,
    write_private,
)
from .garmin_client import GarminLoginRequired, GarminUnavailable, resume, sign_in
from .hevy import HevyClient, HevyError

HEVY_KEY_URL = "https://hevy.com/settings?developer"

# Non-secret hevy2garmin settings. Written to profile.json in the home folder
# and merged into ~/.hevy2garmin/config.json on every setup, because that file
# lives outside the home folder and a new machine starts without it.
DEFAULT_PROFILE: dict[str, Any] = {
    "_comment": (
        "hevy2garmin settings applied by `garmin-hevy-sync setup`. user_profile feeds the "
        "Keytel calorie formula, so wrong values only mean less accurate calorie estimates. "
        "merge_watch_strategy 'merge' keeps the watch's heart rate and training load."
    ),
    "user_profile": {"weight_kg": 70.0, "birth_year": 1990, "sex": "male", "vo2max": 40.0},
    "merge_watch_strategy": "merge",
    "hr_fusion": {"enabled": True},
    # hevy2garmin waits this long after a Hevy workout ends before syncing it,
    # so the watch recording has reached Garmin and gets merged rather than
    # duplicated by a fresh upload.
    "sync": {"skip_existing": True, "grace_period_minutes": 120},
    # This tool schedules hevy2garmin itself; its own scheduler stays off.
    "auto_sync": {"enabled": False},
}


def _say(text: str = "") -> None:
    print(text, flush=True)


def _step(number: int, total: int, title: str) -> None:
    _say(f"\n[{number}/{total}] {title}")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"  {prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _mask(secret: str) -> str:
    return f"...{secret[-4:]}" if len(secret) > 8 else "set"


# --------------------------------------------------------------------- Hevy


def _check_hevy(key: str) -> tuple[bool, str]:
    try:
        info = HevyClient(key).user_info()
    except HevyError as exc:
        if exc.status in (401, 403):
            return False, "Hevy rejected this key. Check it, and that Hevy Pro is active."
        return False, f"Could not reach Hevy: {exc}"
    user = info.get("data", info)
    return True, str(user.get("name") or user.get("username") or "your account")


def _setup_hevy(config_env: Path) -> str | None:
    key = os.environ.get("HEVY_API_KEY", "").strip()
    if key:
        ok, detail = _check_hevy(key)
        if ok:
            _say(f"  Connected as {detail} (key {_mask(key)}).")
            return key
        _say(f"  The saved key does not work: {detail}")
    _say(f"  Create an API key at {HEVY_KEY_URL} (needs Hevy Pro), then paste it here.")
    for _ in range(3):
        key = getpass.getpass("  Hevy API key (input hidden): ").strip()
        if not key:
            continue
        ok, detail = _check_hevy(key)
        if ok:
            update_env_file(config_env, {"HEVY_API_KEY": key})
            os.environ["HEVY_API_KEY"] = key
            _say(f"  Connected as {detail}. Key saved to {config_env}.")
            return key
        _say(f"  {detail}")
    _say("  Skipped. Run setup again once you have a working key.")
    return None


# ------------------------------------------------------------------- Garmin


def _setup_garmin(config_env: Path) -> str:
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    try:
        client = resume()
        _say(f"  Signed in as {client.get_full_name()}.")
        return email
    except GarminUnavailable as exc:
        _say(f"  {exc}")
        _say("  Your existing sign-in was kept; run setup again later to check it.")
        return email
    except GarminLoginRequired:
        pass

    _say("  Sign in once. Garmin may email you a security code. Your password is")
    _say(f"  used for this sign-in only and is never stored; tokens go to {garmin_token_dir()}.")
    email = _ask("Garmin Connect email", email)
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass(
        "  Garmin password (input hidden): "
    )
    try:
        client = sign_in(email, password)
    except Exception as exc:
        _say(f"  Sign-in failed: {exc}")
        if "429" in str(exc) or "too many" in str(exc).lower():
            _say("  Garmin rate-limits sign-ins. Wait 15 minutes before trying again.")
        _say("  Retry with `garmin-hevy-sync login`, then run setup again.")
        return email
    update_env_file(config_env, {"GARMIN_EMAIL": email})
    os.environ["GARMIN_EMAIL"] = email
    _say(f"  Signed in as {client.get_full_name()}.")
    return email


# ------------------------------------------------------------------ profile


def _ask_profile() -> dict[str, Any]:
    profile = copy.deepcopy(DEFAULT_PROFILE)
    body = profile["user_profile"]
    _say("  Used only for hevy2garmin's calorie estimate. Press Enter to keep a value.")
    for field, label, cast in (
        ("weight_kg", "Body weight in kg", float),
        ("birth_year", "Birth year", int),
        ("sex", "Sex for the calorie formula (male/female)", str),
    ):
        while True:
            raw = _ask(label, str(body[field]))
            try:
                value = cast(raw)
            except ValueError:
                _say("  That does not look right, try again.")
                continue
            if field == "sex" and value not in {"male", "female"}:
                _say("  Enter male or female.")
                continue
            body[field] = value
            break
    return profile


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def apply_hevy2garmin_config(
    profile: dict[str, Any], hevy_api_key: str | None, garmin_email: str
) -> list[str]:
    """Write the settings hevy2garmin needs into ~/.hevy2garmin/config.json.

    Edits the JSON file directly instead of going through hevy2garmin's
    load_config(): that overlays environment variables, including
    GARMIN_PASSWORD, and saving its result would write the password to disk.
    Any password already sitting in the file from an older setup is removed.
    """
    notes: list[str] = []
    home = hevy2garmin_home()
    target = home / "config.json"
    try:
        from hevy2garmin.config import DEFAULT_CONFIG

        current: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    except ImportError:
        current = {}
    if target.exists():
        try:
            _deep_merge(current, json.loads(target.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"Could not read {target} ({exc}); rewriting it.")
    settings = {k: v for k, v in profile.items() if not k.startswith("_")}
    _deep_merge(current, settings)
    if hevy_api_key:
        current["hevy_api_key"] = hevy_api_key
    if garmin_email:
        current["garmin_email"] = garmin_email
    current["garmin_token_dir"] = str(garmin_token_dir())
    if current.pop("garmin_password", None):
        notes.append(f"Removed a stored Garmin password from {target}.")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        home.chmod(0o700)
    write_private(target, json.dumps(current, indent=2) + "\n")
    notes.append(f"Wrote hevy2garmin settings to {target}.")
    return notes


def _setup_profile(profile_path: Path, interactive: bool) -> dict[str, Any]:
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            _say(f"  Using {profile_path}.")
            return profile
        except (OSError, json.JSONDecodeError) as exc:
            _say(f"  {profile_path} is unreadable ({exc}); asking again.")
    profile = _ask_profile() if interactive else copy.deepcopy(DEFAULT_PROFILE)
    write_private(profile_path, json.dumps(profile, indent=2) + "\n")
    _say(f"  Saved {profile_path}.")
    return profile


# -------------------------------------------------------------------- main


def run(no_schedule: bool = False, minutes: int = schedule.DEFAULT_MINUTES) -> int:
    if not sys.stdin or not sys.stdin.isatty():
        _say("setup is interactive. Run it in a terminal (in Docker: docker compose run --rm).")
        return 2
    p = ensure_home()
    if not p.config_env.exists():
        write_private(p.config_env, CONFIG_TEMPLATE)
    load_env(p.config_env)

    _say("garmin-hevy-sync setup")
    _say(f"Settings and state live in {p.home}")
    total = 4

    _step(1, total, "Hevy")
    key = _setup_hevy(p.config_env)

    _step(2, total, "Garmin Connect")
    email = _setup_garmin(p.config_env)

    _step(3, total, "Calorie profile")
    profile = _setup_profile(p.profile, interactive=True)
    for note in apply_hevy2garmin_config(profile, key, email):
        _say(f"  {note}")

    _step(4, total, "Background sync")
    scheduled = False
    if running_in_container():
        _say("  The container runs the sync loop itself; nothing to install.")
        scheduled = True
    elif no_schedule:
        _say(
            "  Skipped (--no-schedule). Turn it on later with `garmin-hevy-sync schedule install`."
        )
    elif _confirm(f"Sync in the background every {minutes} minutes?"):
        try:
            result = schedule.install(minutes)
        except (RuntimeError, ConfigError) as exc:
            _say(f"  Could not install the schedule: {exc}")
        else:
            scheduled = True
            for line in result.lines:
                _say(f"  {line}")

    ready = bool(key)
    _say("")
    if not ready:
        _say("Setup is incomplete: the Hevy API key is missing. Run setup again.")
        return 1
    _say("Setup complete.")
    if _confirm("Preview what the first sync would do (nothing is written)?"):
        from .cli import main as cli_main

        cli_main(["sync", "--dry-run"])
    if scheduled:
        _say(
            "\nThe background sync takes it from here. Check on it with `garmin-hevy-sync status`."
        )
    else:
        _say("\nRun `garmin-hevy-sync sync` whenever you want to sync.")
    return 0
