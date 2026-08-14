"""The three flows this repo owns.

Flow B: Garmin watch-recorded strength sessions -> Hevy workouts.
Flow D: Garmin weigh-ins -> Hevy body measurements.
Flow E: repairs flow A's pushed exercise names so Garmin Connect renders them.

Flows A (Hevy workouts -> Garmin activities) and C (Hevy routines -> Garmin
planned workouts) are delegated to the hevy2garmin CLI; see :mod:`gh_sync.cli`.
"""

from __future__ import annotations

import copy
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import state
from .config import HEVY2GARMIN_DB, Settings, hevy2garmin_binary
from .convert import build_hevy_workout
from .exercise_map import ExerciseMapper
from .garmin_client import exercise_sets, parse_start, strength_activities, body_composition
from .hevy import HevyClient, HevyError, extract_workout_id

logger = logging.getLogger("gh_sync.flows")


def _parse_hevy_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recent_hevy_starts(hevy: HevyClient, lookback_days: int) -> list[datetime]:
    """Start times of Hevy workouts inside the window, for overlap detection.

    Workouts come back newest-first, so the scan stops as soon as it walks past
    the window instead of paging through the entire history every run.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days + 1)
    starts: list[datetime] = []
    for workout in hevy.iter_workouts():
        start = _parse_hevy_time(workout.get("start_time"))
        if start is None:
            continue
        if start < cutoff:
            break
        starts.append(start)
    return starts


def _overlaps(start: datetime, hevy_starts: list[datetime], minutes: int) -> bool:
    window = timedelta(minutes=minutes)
    return any(abs(start - other) <= window for other in hevy_starts)


def claimed_garmin_activity_ids(db_path: Path = HEVY2GARMIN_DB) -> set[str]:
    """Garmin activities that flow A has already paired with a Hevy workout.

    The start-time overlap check alone is not enough. hevy2garmin matches within
    +/-30 minutes but also falls back to the same calendar day, so a session
    logged into Hevy hours after the watch recorded it still gets merged
    correctly by flow A. Flow B, comparing only start times, would see no
    nearby Hevy workout and import that same activity a second time.

    Reading the pairing straight out of hevy2garmin's ledger closes that gap
    regardless of how far apart the two timestamps drift. Best-effort: a
    missing or unreadable database just means falling back to the time check.
    """
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT garmin_activity_id FROM synced_workouts "
                "WHERE garmin_activity_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Could not read hevy2garmin ledger at %s: %s", db_path, exc)
        return set()
    return {str(row[0]) for row in rows if row[0]}


def _mark_synced_in_hevy2garmin(hevy_workout_id: str, garmin_activity_id: Any) -> None:
    """Tell hevy2garmin this workout is already on Garmin, closing the loop.

    Without this, flow A would see the workout flow B just created and push it
    back to Garmin on the next run. Best-effort: a failure here is logged, and
    hevy2garmin's own +/-30min matcher is the second line of defence.
    """
    binary = hevy2garmin_binary()
    if not binary:
        logger.warning("hevy2garmin not installed; cannot mark %s as synced", hevy_workout_id)
        return
    try:
        subprocess.run(
            [
                binary, "mark-synced", str(hevy_workout_id),
                "--garmin-id", str(garmin_activity_id),
                "--reason", "created by gh-sync flow B from this Garmin activity",
            ],
            check=True, capture_output=True, timeout=120,
        )
        logger.info("Marked Hevy workout %s as already-synced in hevy2garmin", hevy_workout_id)
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "mark-synced failed for %s: %s", hevy_workout_id,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
    except subprocess.TimeoutExpired:
        logger.warning("mark-synced timed out for %s", hevy_workout_id)


def flow_b_garmin_to_hevy(
    garmin: Any, hevy: HevyClient, conn: sqlite3.Connection, settings: Settings
) -> dict[str, int]:
    """Import watch-recorded strength sessions that Hevy does not already have."""
    counters = {"imported": 0, "skipped": 0, "failed": 0, "considered": 0}

    templates = list(hevy.iter_exercise_templates())
    logger.info("Loaded %s Hevy exercise templates", len(templates))
    template_types = {t["id"]: t.get("type", "") for t in templates if t.get("id")}
    mapper = ExerciseMapper(templates, threshold=settings.match_threshold)

    hevy_starts = recent_hevy_starts(hevy, settings.lookback_days)
    logger.info("Found %s Hevy workouts in the lookback window", len(hevy_starts))

    claimed = claimed_garmin_activity_ids()
    logger.info("%s Garmin activities already paired by flow A", len(claimed))

    for activity in strength_activities(garmin, settings.lookback_days):
        activity_id = str(activity.get("activityId"))
        counters["considered"] += 1

        if state.already_handled(conn, activity_id):
            continue

        if activity_id in claimed:
            state.record(
                conn, activity_id, state.SKIPPED,
                note="already paired with a Hevy workout by hevy2garmin",
            )
            counters["skipped"] += 1
            continue

        start = parse_start(activity)
        if start is None:
            state.record(conn, activity_id, state.SKIPPED, note="no parseable start time")
            counters["skipped"] += 1
            continue

        if _overlaps(start, hevy_starts, settings.overlap_minutes):
            state.record(
                conn, activity_id, state.SKIPPED,
                note=f"a Hevy workout already exists within {settings.overlap_minutes} min",
            )
            counters["skipped"] += 1
            continue

        try:
            sets = exercise_sets(garmin, activity_id)
        except Exception as exc:  # noqa: BLE001 - network/API shape issues are per-activity
            logger.warning("Could not read sets for activity %s: %s", activity_id, exc)
            state.record(conn, activity_id, state.FAILED, note=f"exercise set fetch: {exc}"[:300])
            counters["failed"] += 1
            continue

        if not sets:
            state.record(conn, activity_id, state.SKIPPED, note="activity has no exercise sets")
            counters["skipped"] += 1
            continue

        payload, unmapped = build_hevy_workout(
            activity, sets, mapper, template_types=template_types, start=start
        )
        if unmapped:
            logger.warning("Activity %s has unmapped exercises: %s", activity_id, unmapped)

        if payload is None:
            state.record(
                conn, activity_id, state.SKIPPED,
                note=f"no exercise mapped to a Hevy template ({', '.join(unmapped)})"[:300],
            )
            counters["skipped"] += 1
            continue

        if settings.dry_run:
            logger.info(
                "[dry-run] would create Hevy workout %r with %s exercises from activity %s",
                payload["title"], len(payload["exercises"]), activity_id,
            )
            counters["imported"] += 1
            continue

        try:
            created = hevy.create_workout(payload)
        except HevyError as exc:
            logger.error("Hevy rejected activity %s: %s", activity_id, exc)
            state.record(conn, activity_id, state.FAILED, note=str(exc)[:300])
            counters["failed"] += 1
            continue

        hevy_id = extract_workout_id(created)
        if hevy_id is None:
            logger.warning(
                "Hevy accepted activity %s but returned no workout id; "
                "the overlap check will prevent a re-import", activity_id,
            )
        state.record(
            conn, activity_id, state.IMPORTED, hevy_workout_id=hevy_id,
            note=f"unmapped: {', '.join(unmapped)}" if unmapped else None,
        )
        counters["imported"] += 1
        logger.info("Imported Garmin activity %s as Hevy workout %s", activity_id, hevy_id)

        if hevy_id:
            _mark_synced_in_hevy2garmin(hevy_id, activity_id)
            hevy_starts.append(start)

    mapper.save()
    return counters


# Garmin's own rep detection stores how confident it is that it identified an
# exercise. hevy2garmin pushes exact names but leaves that confidence at 0.0,
# and Garmin Connect reads 0.0 as "nothing identified": the set renders as
# "Choose an Exercise" and the muscle map stays blank even though a valid
# category and name are sitting right there. Restating the names at full
# confidence is what makes them show up. Verified live on activity 23964146255.
NAMED_EXERCISE_CONFIDENCE = 100.0


def _boost_named_exercises(sets: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Copy of ``sets`` with zero-confidence named exercises raised to full.

    Only ``probability`` is ever written. A named exercise carrying no
    confidence is the signature of a programmatic push, so anything the watch
    detected itself (which always carries a real score) and anything without a
    usable category are both left exactly as they are.
    """
    payload = copy.deepcopy(sets)
    boosted = 0
    for entry in payload.get("exerciseSets") or []:
        if (entry.get("setType") or "").upper() != "ACTIVE":
            continue
        for exercise in entry.get("exercises") or []:
            category = exercise.get("category")
            if not category or category == "UNKNOWN":
                continue
            if exercise.get("probability") in (None, 0, 0.0):
                exercise["probability"] = NAMED_EXERCISE_CONFIDENCE
                boosted += 1
    return payload, boosted


def _has_named_exercise(sets: dict[str, Any]) -> bool:
    """True if any active set carries a usable exercise identity."""
    for entry in sets.get("exerciseSets") or []:
        if (entry.get("setType") or "").upper() != "ACTIVE":
            continue
        for exercise in entry.get("exercises") or []:
            category = exercise.get("category")
            if category and category != "UNKNOWN":
                return True
    return False


def flow_e_exercise_names(
    garmin: Any, conn: sqlite3.Connection, settings: Settings
) -> dict[str, int]:
    """Make flow A's pushed exercise names actually render in Garmin Connect."""
    counters = {"checked": 0, "fixed": 0, "failed": 0}

    for activity in strength_activities(garmin, settings.lookback_days):
        activity_id = str(activity.get("activityId"))
        if state.exercise_names_fixed(conn, activity_id):
            continue
        counters["checked"] += 1

        try:
            sets = garmin.get_activity_exercise_sets(activity_id) or {}
        except Exception as exc:  # noqa: BLE001 - per-activity network/API shape
            logger.warning("Could not read exercise sets for %s: %s", activity_id, exc)
            counters["failed"] += 1
            continue

        payload, boosted = _boost_named_exercises(sets)
        if not boosted:
            # Nothing to do. Only stop re-checking once names are actually
            # present: an activity flow A has not enriched yet still has its
            # names coming, and writing it off here would strand it forever.
            if _has_named_exercise(sets):
                state.record_exercise_names_fixed(conn, activity_id)
            continue

        if settings.dry_run:
            logger.info(
                "[dry-run] would restore %s exercise names on activity %s",
                boosted, activity_id,
            )
            counters["fixed"] += 1
            continue

        try:
            garmin.set_activity_exercise_sets(activity_id, payload)
        except Exception as exc:  # noqa: BLE001
            # Deliberately not recorded, so the next run retries.
            logger.warning("Could not restore names on %s: %s", activity_id, exc)
            counters["failed"] += 1
            continue

        state.record_exercise_names_fixed(conn, activity_id)
        counters["fixed"] += 1
        logger.info("Restored %s exercise names on activity %s", boosted, activity_id)

    return counters


def flow_d_body_measurements(
    garmin: Any, hevy: HevyClient, conn: sqlite3.Connection, settings: Settings
) -> dict[str, int]:
    """Copy Garmin weigh-ins into Hevy body measurements."""
    counters = {"synced": 0, "skipped": 0, "failed": 0, "no_date": 0, "considered": 0}

    try:
        entries = body_composition(garmin, settings.body_lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read Garmin body composition: %s", exc)
        return counters

    logger.info(
        "Garmin returned %s weigh-ins in the last %s days",
        len(entries), settings.body_lookback_days,
    )

    for entry in entries:
        counters["considered"] += 1
        measured_on = entry.get("calendarDate")
        if not measured_on:
            # Counted rather than dropped: without this the summary reports the
            # same all-zero line whether Garmin had no weigh-ins at all or
            # returned entries this code could not read.
            logger.warning("Garmin weigh-in has no calendarDate, ignoring: %s", entry)
            counters["no_date"] += 1
            continue
        if state.body_measurement_synced(conn, measured_on):
            counters["skipped"] += 1
            continue

        measurement: dict[str, Any] = {"date": measured_on}
        # Garmin reports masses in grams.
        for source, target in (("weight", "weight_kg"), ("muscleMass", "lean_mass_kg")):
            grams = entry.get(source)
            if isinstance(grams, (int, float)) and grams > 0:
                measurement[target] = round(grams / 1000.0, 2)
        body_fat = entry.get("bodyFat")
        if isinstance(body_fat, (int, float)) and body_fat > 0:
            measurement["fat_percent"] = round(float(body_fat), 2)

        if len(measurement) == 1:  # date only, nothing worth sending
            counters["skipped"] += 1
            continue

        if settings.dry_run:
            logger.info("[dry-run] would post body measurement %s", measurement)
            counters["synced"] += 1
            continue

        try:
            result = hevy.create_body_measurement(measurement)
        except HevyError as exc:
            logger.error("Body measurement for %s rejected: %s", measured_on, exc)
            counters["failed"] += 1
            continue

        state.record_body_measurement(conn, measured_on)
        if result is None:
            logger.info("Hevy already had a measurement for %s", measured_on)
            counters["skipped"] += 1
        else:
            logger.info("Synced body measurement %s to Hevy", measurement)
            counters["synced"] += 1

    return counters
