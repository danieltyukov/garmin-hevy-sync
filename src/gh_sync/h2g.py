"""Running the hevy2garmin CLI, which owns flows A and C.

It runs as a child process rather than in-process: it configures logging and
global state of its own, and a crash inside it should cost one flow, not the
whole run.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from importlib import metadata

from .config import hevy2garmin_command

logger = logging.getLogger("gh_sync.hevy2garmin")

# hevy2garmin reports progress on stderr. Logging all of it as WARNING made a
# healthy run look alarming, so only lines that read like trouble keep that level.
_TROUBLE = ("error", "fail", "traceback", "exception", "denied", "invalid", "429")


class Hevy2GarminMissing(RuntimeError):
    pass


def version() -> str | None:
    try:
        return metadata.version("hevy2garmin")
    except metadata.PackageNotFoundError:
        return None


def child_env() -> dict[str, str]:
    """The environment for the child, minus the Garmin password.

    hevy2garmin falls back to a password sign-in when its tokens are rejected.
    Unattended, that cannot answer an MFA prompt and only earns a 429 from
    Garmin, so it never gets the password from us.
    """
    env = dict(os.environ)
    env.pop("GARMIN_PASSWORD", None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _creationflags() -> int:
    # Without this, a windowless scheduled run on Windows would pop a console
    # window for every child process.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


# A normal hevy2garmin run takes seconds. Fifteen minutes is generous and stays
# well under the schedulers' own kill limits (60 minutes), so a hung child is
# stopped here and the run is still recorded and reported.
CHILD_TIMEOUT_SECONDS = 15 * 60


def run(args: list[str], timeout: int = CHILD_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    command = hevy2garmin_command()
    if command is None:
        raise Hevy2GarminMissing("hevy2garmin is not installed in this environment")
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env(),
        creationflags=_creationflags(),
        stdin=subprocess.DEVNULL,
    )


def run_logged(args: list[str], dry_run: bool = False) -> bool:
    """Run a hevy2garmin command, forwarding its output to our log."""
    if dry_run and "--dry-run" not in args:
        args = [*args, "--dry-run"]
    logger.info("Running hevy2garmin %s", " ".join(args))
    try:
        result = run(args)
    except Hevy2GarminMissing as exc:
        logger.error("%s", exc)
        return False
    except subprocess.TimeoutExpired:
        logger.error("hevy2garmin %s timed out", args[0])
        return False
    for line in (result.stdout or "").splitlines():
        if line.strip():
            logger.info("%s", line.rstrip())
    for line in (result.stderr or "").splitlines():
        if not line.strip():
            continue
        level = logging.WARNING if any(t in line.lower() for t in _TROUBLE) else logging.INFO
        logger.log(level, "%s", line.rstrip())
    if result.returncode != 0:
        logger.error("hevy2garmin %s exited with status %s", args[0], result.returncode)
        return False
    return True


def mark_synced(hevy_workout_id: str, garmin_activity_id: str) -> bool:
    """Record in hevy2garmin's ledger that a Hevy workout is already on Garmin."""
    try:
        result = run(
            [
                "mark-synced",
                str(hevy_workout_id),
                "--garmin-id",
                str(garmin_activity_id),
                "--reason",
                "created by garmin-hevy-sync flow B from this Garmin activity",
            ],
            timeout=120,
        )
    except (Hevy2GarminMissing, subprocess.TimeoutExpired) as exc:
        logger.warning("mark-synced failed for %s: %s", hevy_workout_id, exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "mark-synced failed for %s: %s", hevy_workout_id, (result.stderr or "")[:300]
        )
        return False
    logger.info("Marked Hevy workout %s as already synced in hevy2garmin", hevy_workout_id)
    return True
