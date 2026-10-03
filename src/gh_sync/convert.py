"""Pure conversion from Garmin exercise sets to a Hevy workout payload.

Kept free of network calls so the grouping and unit handling can be tested
directly. Everything that talks to an API lives in :mod:`gh_sync.flows`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .exercise_map import ExerciseMapper, garmin_key

# Marker written into every workout description so the pairing is auditable
# from inside the Hevy app, and so a human can tell what created the entry.
SOURCE_MARKER = "Imported from Garmin activity"

# Hevy template types that accept a weight value. Sending weight to a
# reps-only template is silently dropped at best, so gate on this.
WEIGHTED_TYPES = {
    "weight_reps",
    "weighted_bodyweight",
    "assisted_bodyweight",
    "duration_weight",
    "weight_distance",
}
DURATION_TYPES = {"duration", "duration_weight", "distance_duration"}
DISTANCE_TYPES = {"distance_duration", "weight_distance"}


def _primary_exercise(garmin_set: dict[str, Any]) -> tuple[str | None, str | None]:
    """Highest-probability exercise guess attached to a set."""
    exercises = garmin_set.get("exercises") or []
    if not exercises:
        return None, None
    best = max(exercises, key=lambda e: e.get("probability") or 0)
    return best.get("category"), best.get("name")


def group_consecutive(
    sets: Iterable[dict[str, Any]],
) -> list[tuple[tuple[str | None, str | None], list[dict]]]:
    """Collapse a flat set list into consecutive runs of the same exercise.

    Runs rather than a global group-by: an A/B/A/B superset stays in recorded
    order as four entries instead of being silently reordered into two.
    """
    groups: list[tuple[tuple[str | None, str | None], list[dict]]] = []
    for garmin_set in sets:
        identity = _primary_exercise(garmin_set)
        if groups and groups[-1][0] == identity:
            groups[-1][1].append(garmin_set)
        else:
            groups.append((identity, [garmin_set]))
    return groups


def _build_set(garmin_set: dict[str, Any], template_type: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "normal"}

    reps = garmin_set.get("repetitionCount")
    if isinstance(reps, (int, float)) and reps > 0:
        payload["reps"] = int(reps)

    # Garmin reports weight in grams; Hevy wants kilograms.
    grams = garmin_set.get("weight")
    if (
        isinstance(grams, (int, float))
        and grams > 0
        and (template_type is None or template_type in WEIGHTED_TYPES)
    ):
        payload["weight_kg"] = round(grams / 1000.0, 2)

    duration = garmin_set.get("duration")
    if isinstance(duration, (int, float)) and duration > 0 and template_type in DURATION_TYPES:
        payload["duration_seconds"] = round(duration)

    distance = garmin_set.get("distance")
    if isinstance(distance, (int, float)) and distance > 0 and template_type in DISTANCE_TYPES:
        payload["distance_meters"] = round(distance)

    return payload


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_METRIC_FIELDS = ("reps", "weight_kg", "duration_seconds", "distance_meters")


def _is_blank(built_set: dict[str, Any]) -> bool:
    """A set carrying no measurement at all, only its type."""
    return not any(field in built_set for field in _METRIC_FIELDS)


def _blank_set_note(group: list[dict[str, Any]]) -> str:
    """Explain a run of measurement-free sets, keeping whatever Garmin did record.

    The watch auto-detects the movement but frequently reports repetitionCount
    as 0 with no weight, which would otherwise reach Hevy as silent empty rows.
    The per-set durations are the one thing it did capture, and a weight_reps
    template has nowhere to put them, so they go in the exercise note instead.
    """
    durations = [
        round(item["duration"])
        for item in group
        if isinstance(item.get("duration"), (int, float)) and item["duration"] > 0
    ]
    note = "Garmin recorded no reps or weight for these sets."
    if durations:
        note += " Set durations: " + ", ".join(f"{value}s" for value in durations) + "."
    return note


def build_hevy_workout(
    activity: dict[str, Any],
    sets: list[dict[str, Any]],
    mapper: ExerciseMapper,
    template_types: dict[str, str] | None = None,
    start: datetime | None = None,
    private: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Turn one Garmin strength activity into a Hevy workout payload.

    Returns ``(payload, unmapped_keys)``. ``payload`` is None when nothing in
    the activity could be mapped, so the caller can record a skip rather than
    POST an empty workout.
    """
    template_types = template_types or {}
    exercises: list[dict[str, Any]] = []
    unmapped: list[str] = []
    blank: list[str] = []

    for (category, name), group in group_consecutive(sets):
        match = mapper.resolve(category, name)
        if match is None:
            key = garmin_key(category, name)
            if key not in unmapped:
                unmapped.append(key)
            continue
        template_type = template_types.get(match.hevy_id)
        built_sets = [_build_set(s, template_type) for s in group]

        notes = None
        if built_sets and all(_is_blank(s) for s in built_sets):
            notes = _blank_set_note(group)
            if match.hevy_title not in blank:
                blank.append(match.hevy_title)

        exercises.append(
            {
                "exercise_template_id": match.hevy_id,
                "superset_id": None,
                "notes": notes,
                "sets": built_sets,
            }
        )

    if not exercises:
        return None, unmapped

    activity_id = activity.get("activityId")
    duration_seconds = activity.get("duration") or activity.get("elapsedDuration") or 0
    if start is None:
        start = datetime.now(UTC)
    end = start + timedelta(seconds=float(duration_seconds or 0))

    description = f"{SOURCE_MARKER} {activity_id}."
    if unmapped:
        description += " Unmapped exercises left out: " + ", ".join(unmapped) + "."
    if blank:
        description += " Needs reps and weight filling in: " + ", ".join(blank) + "."

    workout = {
        "title": activity.get("activityName") or "Strength Training",
        "description": description,
        "start_time": _iso_z(start),
        "end_time": _iso_z(end),
        "is_private": private,
        "exercises": exercises,
    }
    return workout, unmapped
