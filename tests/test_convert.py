from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gh_sync.convert import SOURCE_MARKER, build_hevy_workout, group_consecutive
from gh_sync.exercise_map import ExerciseMapper

TEMPLATES = [
    {"id": "T_BENCH_BB", "title": "Bench Press (Barbell)", "type": "weight_reps"},
    {"id": "T_SQUAT_BB", "title": "Squat (Barbell)", "type": "weight_reps"},
    {"id": "T_PLANK", "title": "Plank", "type": "duration"},
    {"id": "T_PULL_UP", "title": "Pull Up", "type": "reps_only"},
]
TEMPLATE_TYPES = {t["id"]: t["type"] for t in TEMPLATES}

START = datetime(2026, 7, 29, 17, 30, tzinfo=UTC)


def a_set(category, name, reps=10, grams=60000.0, duration=45.0, set_type="ACTIVE"):
    return {
        "exercises": [{"category": category, "name": name, "probability": 100.0}],
        "repetitionCount": reps,
        "weight": grams,
        "duration": duration,
        "setType": set_type,
    }


ACTIVITY = {
    "activityId": 987654321,
    "activityName": "Upper Body",
    "duration": 3600.0,
}


@pytest.fixture()
def mapper(tmp_path):
    return ExerciseMapper(TEMPLATES, threshold=0.55, map_file=tmp_path / "map.json")


class TestGroupConsecutive:
    def test_consecutive_sets_collapse(self):
        sets = [a_set("BENCH_PRESS", "BARBELL_BENCH_PRESS") for _ in range(3)]
        groups = group_consecutive(sets)
        assert len(groups) == 1
        assert len(groups[0][1]) == 3

    def test_superset_keeps_recorded_order(self):
        sets = [
            a_set("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
            a_set("SQUAT", "BARBELL_SQUAT"),
            a_set("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
        ]
        groups = group_consecutive(sets)
        assert [g[0][0] for g in groups] == ["BENCH_PRESS", "SQUAT", "BENCH_PRESS"]

    def test_empty_input(self):
        assert group_consecutive([]) == []

    def test_set_with_no_exercise_metadata(self):
        groups = group_consecutive([{"repetitionCount": 5}])
        assert groups == [((None, None), [{"repetitionCount": 5}])]

    def test_highest_probability_exercise_wins(self):
        ambiguous = {
            "exercises": [
                {"category": "SQUAT", "name": "BARBELL_SQUAT", "probability": 20.0},
                {"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 80.0},
            ],
            "repetitionCount": 8,
        }
        assert group_consecutive([ambiguous])[0][0] == ("BENCH_PRESS", "BARBELL_BENCH_PRESS")


class TestBuildHevyWorkout:
    def test_happy_path(self, mapper):
        sets = [a_set("BENCH_PRESS", "BARBELL_BENCH_PRESS", reps=8, grams=80000.0)] * 3
        payload, unmapped = build_hevy_workout(ACTIVITY, sets, mapper, TEMPLATE_TYPES, start=START)
        assert unmapped == []
        assert payload["title"] == "Upper Body"
        assert payload["start_time"] == "2026-07-29T17:30:00Z"
        assert payload["end_time"] == "2026-07-29T18:30:00Z"
        assert len(payload["exercises"]) == 1
        exercise = payload["exercises"][0]
        assert exercise["exercise_template_id"] == "T_BENCH_BB"
        assert len(exercise["sets"]) == 3
        assert exercise["sets"][0] == {"type": "normal", "reps": 8, "weight_kg": 80.0}

    def test_grams_convert_to_kilograms(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("SQUAT", "BARBELL_SQUAT", grams=102500.0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert payload["exercises"][0]["sets"][0]["weight_kg"] == 102.5

    def test_weight_omitted_for_reps_only_template(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("PULL_UP", "PULL_UP", reps=12, grams=5000.0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        built = payload["exercises"][0]["sets"][0]
        assert built["reps"] == 12
        assert "weight_kg" not in built

    def test_duration_only_for_duration_templates(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("PLANK", "PLANK", reps=0, grams=0, duration=90.0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        built = payload["exercises"][0]["sets"][0]
        assert built["duration_seconds"] == 90
        assert "reps" not in built

    def test_bench_press_does_not_get_a_duration(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("BENCH_PRESS", "BARBELL_BENCH_PRESS", duration=45.0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert "duration_seconds" not in payload["exercises"][0]["sets"][0]

    def test_zero_weight_is_dropped(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("SQUAT", "BARBELL_SQUAT", grams=0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert "weight_kg" not in payload["exercises"][0]["sets"][0]

    def test_null_weight_is_dropped(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("SQUAT", "BARBELL_SQUAT", grams=None)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert "weight_kg" not in payload["exercises"][0]["sets"][0]

    def test_description_carries_the_source_marker(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY, [a_set("SQUAT", "BARBELL_SQUAT")], mapper, TEMPLATE_TYPES, start=START
        )
        assert SOURCE_MARKER in payload["description"]
        assert "987654321" in payload["description"]

    def test_unmapped_exercises_are_listed_not_silently_dropped(self, mapper):
        sets = [
            a_set("SQUAT", "BARBELL_SQUAT"),
            a_set("UNDERWATER_BASKET_WEAVING", "UNDERWATER_BASKET_WEAVING"),
        ]
        payload, unmapped = build_hevy_workout(ACTIVITY, sets, mapper, TEMPLATE_TYPES, start=START)
        assert unmapped == ["UNDERWATER_BASKET_WEAVING"]
        assert len(payload["exercises"]) == 1
        assert "UNDERWATER_BASKET_WEAVING" in payload["description"]

    def test_nothing_mappable_returns_none(self, mapper):
        payload, unmapped = build_hevy_workout(
            ACTIVITY, [a_set("QQQQQ_ZZZZZ", "QQQQQ_ZZZZZ")], mapper, TEMPLATE_TYPES, start=START
        )
        assert payload is None
        assert unmapped == ["QQQQQ_ZZZZZ"]

    def test_missing_activity_name_falls_back(self, mapper):
        payload, _ = build_hevy_workout(
            {"activityId": 1, "duration": 60},
            [a_set("SQUAT", "BARBELL_SQUAT")],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert payload["title"] == "Strength Training"

    def test_missing_duration_gives_zero_length_workout(self, mapper):
        payload, _ = build_hevy_workout(
            {"activityId": 1, "activityName": "X"},
            [a_set("SQUAT", "BARBELL_SQUAT")],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert payload["start_time"] == payload["end_time"]

    def test_sets_with_no_metrics_keep_their_durations_in_a_note(self, mapper):
        # The Venu 4 auto-detects the movement but often reports 0 reps and no
        # weight. The durations are all it captured, and a weight_reps template
        # has no field for them.
        sets = [
            a_set("SQUAT", "BARBELL_SQUAT", reps=0, grams=None, duration=20.4),
            a_set("SQUAT", "BARBELL_SQUAT", reps=0, grams=None, duration=11.8),
        ]
        payload, _ = build_hevy_workout(ACTIVITY, sets, mapper, TEMPLATE_TYPES, start=START)
        exercise = payload["exercises"][0]
        assert exercise["sets"] == [{"type": "normal"}, {"type": "normal"}]
        assert "20s" in exercise["notes"] and "12s" in exercise["notes"]
        assert "Squat (Barbell)" in payload["description"]

    def test_blank_note_omitted_when_metrics_exist(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("SQUAT", "BARBELL_SQUAT", reps=5, grams=60000.0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        assert payload["exercises"][0]["notes"] is None
        assert "filling in" not in payload["description"]

    def test_blank_note_survives_absent_durations(self, mapper):
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("SQUAT", "BARBELL_SQUAT", reps=0, grams=None, duration=0)],
            mapper,
            TEMPLATE_TYPES,
            start=START,
        )
        notes = payload["exercises"][0]["notes"]
        assert "no reps or weight" in notes
        assert "durations" not in notes

    def test_partially_blank_exercise_is_not_flagged(self, mapper):
        # One real set among blanks means the exercise carries data.
        sets = [
            a_set("SQUAT", "BARBELL_SQUAT", reps=0, grams=None),
            a_set("SQUAT", "BARBELL_SQUAT", reps=5, grams=60000.0),
        ]
        payload, _ = build_hevy_workout(ACTIVITY, sets, mapper, TEMPLATE_TYPES, start=START)
        assert payload["exercises"][0]["notes"] is None

    def test_unknown_template_type_still_sends_weight(self, mapper):
        # template_types empty: we cannot tell, so send what Garmin gave us
        payload, _ = build_hevy_workout(
            ACTIVITY,
            [a_set("SQUAT", "BARBELL_SQUAT", grams=100000.0)],
            mapper,
            template_types={},
            start=START,
        )
        assert payload["exercises"][0]["sets"][0]["weight_kg"] == 100.0
