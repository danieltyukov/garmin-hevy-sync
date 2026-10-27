"""Keeping hevy2garmin's calorie profile weight current.

hevy2garmin estimates the calories of every workout it writes to Garmin from
heart rate, age, VO2max and body weight, taken from the profile in its
config.json. Only `setup` ever wrote that weight, so it drifted from the scale,
and those calories now count towards Garmin's nutrition budget. Before flow A
runs, the newest weigh-in in Hevy replaces it. Hevy holds Garmin's weigh-ins
too, since flow D copies them over.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any

from .config import hevy2garmin_home, write_private

logger = logging.getLogger("gh_sync.calorie_profile")

# Hevy lists measurements newest first, so the first page holds the latest.
_SCAN = 10


def latest_weight(hevy: Any) -> tuple[str, float] | None:
    """(date, kg) of the newest Hevy measurement that has a weight."""
    weighed = [
        (m["date"], float(m["weight_kg"]))
        for m in itertools.islice(hevy.iter_body_measurements(), _SCAN)
        if m.get("date") and isinstance(m.get("weight_kg"), (int, float)) and m["weight_kg"] > 0
    ]
    return max(weighed) if weighed else None


def refresh_weight(hevy: Any, dry_run: bool = False) -> float | None:
    """Set hevy2garmin's profile weight to the newest Hevy weigh-in.

    Returns the new weight when it changed. Never raises: a stale weight only
    skews calories, so it must not cost flow A its run.
    """
    target = hevy2garmin_home() / "config.json"
    try:
        if not target.exists():
            return None
        newest = latest_weight(hevy)
        if newest is None:
            return None
        day, weight = newest
        # Edited as plain JSON, not through hevy2garmin's load_config(), for the
        # same reason as setup: that overlays GARMIN_PASSWORD from the environment.
        current = json.loads(target.read_text(encoding="utf-8"))
        profile = current.setdefault("user_profile", {})
        old = profile.get("weight_kg")
        if isinstance(old, (int, float)) and abs(old - weight) < 0.05:
            return None
        if dry_run:
            logger.info("Would set hevy2garmin's profile weight from %s to %.1f kg", old, weight)
            return None
        profile["weight_kg"] = round(weight, 1)
        write_private(target, json.dumps(current, indent=2) + "\n")
    except Exception as exc:
        logger.warning("Could not refresh hevy2garmin's profile weight: %s", exc)
        return None
    logger.info(
        "Set hevy2garmin's profile weight from %s to %s kg (Hevy weigh-in of %s)",
        old,
        profile["weight_kg"],
        day,
    )
    return profile["weight_kg"]
