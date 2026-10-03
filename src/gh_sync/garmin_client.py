"""Garmin Connect access via python-garminconnect.

Garmin has no public consumer API. garminconnect signs in through the same SSO
flow as the mobile app and caches OAuth tokens in a token store (by default
``~/.garminconnect``, shared with hevy2garmin). The tokens refresh themselves,
so the password is only needed for the one interactive ``login``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from .config import garmin_token_dir

logger = logging.getLogger("gh_sync.garmin")

# Garmin activityType.typeKey values that carry exercise sets worth importing.
STRENGTH_TYPE_KEYS = {"strength_training", "indoor_cardio"}


class GarminLoginRequired(RuntimeError):
    """Raised when a fresh interactive login is needed and cannot be done here."""


class GarminUnavailable(RuntimeError):
    """Garmin could not be reached or refused for now; the next run retries."""


def resume() -> Garmin:
    """A client running on the cached tokens, for every unattended path.

    Built without credentials on purpose. Given a password, garminconnect
    falls back to a full sign-in whenever the token store is missing or
    rejected. Under a scheduler nobody can answer the emailed MFA code, and
    repeating that every 30 minutes earns a 429 from Garmin's SSO, which then
    blocks the manual sign-in too. Failing fast with a clear instruction is
    the better outcome.
    """
    tokens = garmin_token_dir()
    client = Garmin()
    try:
        client.login(str(tokens))
    except GarminConnectTooManyRequestsError as exc:
        raise GarminUnavailable(
            "Garmin is rate limiting this IP address. It clears on its own; the next "
            "scheduled run will try again."
        ) from exc
    except GarminConnectAuthenticationError as exc:
        raise GarminLoginRequired(
            f"Garmin sign-in needed: the token store at {tokens} is missing or expired. "
            "Run `garmin-hevy-sync login` in a terminal."
        ) from exc
    except Exception as exc:
        if not has_token_store():
            raise GarminLoginRequired(
                f"Garmin sign-in needed: no token store at {tokens}. "
                "Run `garmin-hevy-sync login` in a terminal."
            ) from exc
        # Tokens exist, so this is the network or Garmin itself, not the
        # sign-in. Saying "log in again" here would send people chasing a
        # problem they do not have.
        raise GarminUnavailable(f"Could not reach Garmin Connect: {exc}") from exc
    logger.info("Garmin session resumed from %s", tokens)
    return client


def has_token_store() -> bool:
    tokens = garmin_token_dir()
    return (tokens.is_dir() and any(tokens.iterdir())) or tokens.is_file()


def sign_in(email: str, password: str, prompt_mfa: Callable[[], str] | None = None) -> Garmin:
    """Full interactive sign-in. Writes fresh tokens to the token store."""
    tokens = garmin_token_dir()
    tokens.mkdir(parents=True, exist_ok=True)
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=prompt_mfa or prompt_for_mfa_code,
        return_on_mfa=False,
    )
    # With a token store path, garminconnect tries the cached tokens first,
    # falls back to the credentials, and writes the new tokens back itself.
    client.login(str(tokens))
    logger.info("Garmin tokens written to %s", tokens)
    return client


def prompt_for_mfa_code() -> str:
    """Read the emailed one-time code from the terminal."""
    print("\nGarmin sent a security code to your email.")
    code = input("Security code: ").strip()
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
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    logger.warning("Unparseable startTimeGMT %r on activity %s", raw, activity.get("activityId"))
    return None


def activity_end(activity: dict[str, Any], start: datetime) -> datetime:
    """When an activity finished: its start plus the elapsed time Garmin recorded."""
    seconds = activity.get("elapsedDuration") or activity.get("duration") or 0
    try:
        return start + timedelta(seconds=float(seconds))
    except (TypeError, ValueError):
        return start


def strength_activities(client: Garmin, lookback_days: int) -> list[dict[str, Any]]:
    """Strength-type activities in the lookback window, oldest first.

    ``get_activities_by_date`` only filters on a fixed set of type keys that
    excludes strength_training, so the filtering happens here instead.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    strength = [
        a for a in activities if (a.get("activityType") or {}).get("typeKey") in STRENGTH_TYPE_KEYS
    ]
    strength.sort(key=lambda a: a.get("startTimeGMT") or "")
    logger.info(
        "Garmin returned %s activities since %s, %s of them strength",
        len(activities),
        start,
        len(strength),
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
