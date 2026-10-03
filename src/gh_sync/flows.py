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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import h2g, state
from .config import Settings, hevy2garmin_db
from .convert import build_hevy_workout
from .exercise_map import ExerciseMapper
from .garmin_client import (
    activity_end,
    body_composition,
    exercise_sets,
    parse_start,
    strength_activities,
)
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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


Session = tuple[datetime, datetime]


def recent_hevy_sessions(
    hevy: HevyClient, lookback_days: int, now: datetime | None = None
) -> list[Session]:
    """(start, end) of Hevy workouts inside the window, for overlap detection.

    Workouts come back newest-first, so the scan stops as soon as it walks past
    the window instead of paging through the entire history every run.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=lookback_days + 1)
    sessions: list[Session] = []
    for workout in hevy.iter_workouts():
        start = _parse_hevy_time(workout.get("start_time"))
        if start is None:
            continue
        if start < cutoff:
            break
        end = _parse_hevy_time(workout.get("end_time")) or start
        sessions.append((start, max(start, end)))
    return sessions


def _overlaps(start: datetime, hevy_starts: list[datetime], minutes: int) -> bool:
    window = timedelta(minutes=minutes)
    return any(abs(start - other) <= window for other in hevy_starts)


def _same_session(start: datetime, end: datetime, sessions: list[Session], minutes: int) -> bool:
    """True if a Hevy workout is the same gym session as this Garmin activity.

    Either the two started within ``minutes`` of each other, or their time
    ranges intersect at all. The second test catches a Hevy workout started
    well into a session the watch had been recording for a while, which the
    start-time window alone would treat as a different workout.
    """
    if _overlaps(start, [s for s, _ in sessions], minutes):
        return True
    return any(start <= h_end and h_start <= end for h_start, h_end in sessions)


def claimed_garmin_activity_ids(db_path: Path | None = None) -> set[str]:
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
    db_path = db_path or hevy2garmin_db()
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


def flow_b_garmin_to_hevy(
    garmin: Any,
    hevy: HevyClient,
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, int]:
    """Import watch-recorded strength sessions that Hevy does not already have."""
    counters = {"imported": 0, "skipped": 0, "failed": 0, "deferred": 0, "considered": 0}
    now = now or datetime.now(UTC)

    templates = list(hevy.iter_exercise_templates())
    logger.info("Loaded %s Hevy exercise templates", len(templates))
    template_types = {t["id"]: t.get("type", "") for t in templates if t.get("id")}
    mapper = ExerciseMapper(templates, threshold=settings.match_threshold)

    hevy_sessions = recent_hevy_sessions(hevy, settings.lookback_days, now)
    logger.info("Found %s Hevy workouts in the lookback window", len(hevy_sessions))

    claimed = claimed_garmin_activity_ids()
    logger.info("%s Garmin activities already paired by flow A", len(claimed))

    for activity in strength_activities(garmin, settings.lookback_days):
        activity_id = str(activity.get("activityId"))
        counters["considered"] += 1

        if state.already_handled(conn, activity_id):
            continue

        if activity_id in claimed:
            state.record(
                conn,
                activity_id,
                state.SKIPPED,
                note="already paired with a Hevy workout by hevy2garmin",
            )
            counters["skipped"] += 1
            continue

        start = parse_start(activity)
        if start is None:
            state.record(conn, activity_id, state.SKIPPED, note="no parseable start time")
            counters["skipped"] += 1
            continue

        end = activity_end(activity, start)
        if _same_session(start, end, hevy_sessions, settings.overlap_minutes):
            state.record(
                conn,
                activity_id,
                state.SKIPPED,
                note="a Hevy workout already covers this session",
            )
            counters["skipped"] += 1
            continue

        # Not recorded: a deferred session is simply looked at again next run.
        ready_at = end + timedelta(minutes=settings.import_delay_minutes)
        if now < ready_at:
            logger.info(
                "Activity %s ended recently; waiting until %s in case it is saved in Hevy",
                activity_id,
                ready_at.isoformat(timespec="minutes"),
            )
            counters["deferred"] += 1
            continue

        try:
            sets = exercise_sets(garmin, activity_id)
        except Exception as exc:  # one bad activity must not stop the rest
            logger.warning("Could not read sets for activity %s: %s", activity_id, exc)
            state.record(conn, activity_id, state.FAILED, note=f"exercise set fetch: {exc}"[:300])
            counters["failed"] += 1
            continue

        if not sets:
            state.record(conn, activity_id, state.SKIPPED, note="activity has no exercise sets")
            counters["skipped"] += 1
            continue

        payload, unmapped = build_hevy_workout(
            activity,
            sets,
            mapper,
            template_types=template_types,
            start=start,
            private=settings.import_private,
        )
        if unmapped:
            logger.warning("Activity %s has unmapped exercises: %s", activity_id, unmapped)

        if payload is None:
            state.record(
                conn,
                activity_id,
                state.SKIPPED,
                note=f"no exercise mapped to a Hevy template ({', '.join(unmapped)})"[:300],
            )
            counters["skipped"] += 1
            continue

        if settings.dry_run:
            logger.info(
                "[dry-run] would create Hevy workout %r with %s exercises from activity %s",
                payload["title"],
                len(payload["exercises"]),
                activity_id,
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
                "the overlap check will prevent a re-import",
                activity_id,
            )
        state.record(
            conn,
            activity_id,
            state.IMPORTED,
            hevy_workout_id=hevy_id,
            note=f"unmapped: {', '.join(unmapped)}" if unmapped else None,
        )
        counters["imported"] += 1
        logger.info("Imported Garmin activity %s as Hevy workout %s", activity_id, hevy_id)

        if hevy_id:
            h2g.mark_synced(hevy_id, activity_id)
            hevy_sessions.append((start, end))

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
        except Exception as exc:
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
                boosted,
                activity_id,
            )
            counters["fixed"] += 1
            continue

        try:
            garmin.set_activity_exercise_sets(activity_id, payload)
        except Exception as exc:
            # Deliberately not recorded, so the next run retries.
            logger.warning("Could not restore names on %s: %s", activity_id, exc)
            counters["failed"] += 1
            continue

        state.record_exercise_names_fixed(conn, activity_id)
        counters["fixed"] += 1
        logger.info("Restored %s exercise names on activity %s", boosted, activity_id)

    return counters


def first_reading_per_day(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One weigh-in per calendar date: the earliest, by Garmin's timestamp.

    Hevy keeps one body measurement per date and answers a second with 409,
    so which reading wins used to depend on the order Garmin happened to list
    them. The first of the day is the conventional one to track. Entries
    without a date pass through so the caller can count them.
    """
    chosen: dict[str, dict[str, Any]] = {}
    undated: list[dict[str, Any]] = []
    for entry in entries:
        day = entry.get("calendarDate")
        if not day:
            undated.append(entry)
            continue
        current = chosen.get(day)
        stamp = entry.get("date") or entry.get("timestampGMT") or 0
        if current is None or stamp < (current.get("date") or current.get("timestampGMT") or 0):
            chosen[day] = entry
    return [chosen[day] for day in sorted(chosen)] + undated


def flow_d_body_measurements(
    garmin: Any, hevy: HevyClient, conn: sqlite3.Connection, settings: Settings
) -> dict[str, int]:
    """Copy Garmin weigh-ins into Hevy body measurements."""
    counters = {"synced": 0, "skipped": 0, "failed": 0, "no_date": 0, "considered": 0}

    # A failure to read the list at all propagates: the run reports flow D as
    # failed instead of an all-zero summary that looks like "no weigh-ins".
    entries = body_composition(garmin, settings.body_lookback_days)

    logger.info(
        "Garmin returned %s weigh-ins in the last %s days",
        len(entries),
        settings.body_lookback_days,
    )

    for entry in first_reading_per_day(entries):
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
