"""Garmin Connect access via python-garminconnect / garth.

Garmin has no public consumer API. ``garth`` authenticates through the mobile
SSO flow and caches OAuth tokens under ``~/.garminconnect``; they refresh
themselves indefinitely, so the password is only touched when the cache is
missing or the refresh token has finally expired.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from garminconnect import Garmin

from .config import GARMIN_TOKENS

logger = logging.getLogger("gh_sync.garmin")

# Garmin activityType.typeKey values that carry exercise sets worth importing.
STRENGTH_TYPE_KEYS = {"strength_training", "indoor_cardio"}


class GarminLoginRequired(RuntimeError):
    """Raised when a fresh interactive login is needed and cannot be done here."""


def connect(email: str, password: str, interactive: bool = False) -> Garmin:
    """Return a logged-in client, preferring cached tokens over the password.

    With MFA enabled on the account, a full login blocks on a one-time code
    delivered by email. That is fine at a terminal and fatal under systemd,
    where nothing can answer the prompt and the unit would sit at its 30 minute
    timeout. So the unattended path refuses to attempt a full login at all and
    tells the operator to run ``gh-sync login`` instead.
    """
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=_prompt_for_mfa_code if interactive else None,
        return_on_mfa=False,
    )

    try:
        client.login(str(GARMIN_TOKENS))
        logger.info("Garmin session resumed from %s", GARMIN_TOKENS)
        return client
    except Exception as exc:  # noqa: BLE001 - garth raises a wide range here
        if not interactive:
            raise GarminLoginRequired(
                f"Garmin token store at {GARMIN_TOKENS} is missing or expired ({exc}). "
                "Run 'gh-sync login' from a terminal to sign in and cache new tokens."
            ) from exc
        logger.info("Token resume failed (%s); starting a full login", exc)

    client.login()
    GARMIN_TOKENS.mkdir(parents=True, exist_ok=True)
    client.garth.dump(str(GARMIN_TOKENS))
    logger.info("Garmin tokens written to %s", GARMIN_TOKENS)
    return client


def _prompt_for_mfa_code() -> str:
    """Read the emailed one-time code from the terminal."""
    print("\nGarmin sent a security code to your email.")
    code = input("Enter the Garmin security code: ").strip()
    if not code:
        raise RuntimeError("No security code entered")
    return code


def parse_start(activity: dict[str, Any]) -> datetime | None:
    """UTC start time of an activity.

    Garmin returns ``startTimeGMT`` as a naive 'YYYY-MM-DD HH:MM:SS' string that
    is actually UTC, so it has to be stamped rather than parsed as local.
    """
    raw = activity.get("startTimeGMT")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("Unparseable startTimeGMT %r on activity %s", raw, activity.get("activityId"))
    return None


def strength_activities(client: Garmin, lookback_days: int) -> list[dict[str, Any]]:
    """Strength-type activities in the lookback window, oldest first.

    ``get_activities_by_date`` only filters on a fixed set of type keys that
    excludes strength_training, so the filtering happens here instead.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    strength = [
        a
        for a in activities
        if (a.get("activityType") or {}).get("typeKey") in STRENGTH_TYPE_KEYS
    ]
    strength.sort(key=lambda a: a.get("startTimeGMT") or "")
    logger.info(
        "Garmin returned %s activities since %s, %s of them strength",
        len(activities), start, len(strength),
    )
    return strength


def exercise_sets(client: Garmin, activity_id: Any) -> list[dict[str, Any]]:
    """Active (non-rest) sets for an activity, in recorded order."""
    payload = client.get_activity_exercise_sets(activity_id) or {}
    sets = payload.get("exerciseSets") or []
    return [s for s in sets if (s.get("setType") or "").upper() != "REST"]


def body_composition(client: Garmin, lookback_days: int) -> list[dict[str, Any]]:
    """Weigh-ins in the lookback window. Weights come back in grams."""
    end = date.today()
    start = end - timedelta(days=lookback_days)
    payload = client.get_body_composition(start.isoformat(), end.isoformat()) or {}
    return payload.get("dateWeightList") or []
