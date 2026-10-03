"""Flow B end to end, with fake Garmin and Hevy clients."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from gh_sync import flows, state
from gh_sync.config import Settings
from gh_sync.flows import first_reading_per_day, flow_b_garmin_to_hevy

TEMPLATES = [
    {"id": "T_BENCH_BB", "title": "Bench Press (Barbell)", "type": "weight_reps"},
    {"id": "T_SQUAT_BB", "title": "Squat (Barbell)", "type": "weight_reps"},
]

# Relative to the real clock: the Garmin and Hevy lookback windows are computed from today.
STARTED = (datetime.now(UTC) - timedelta(hours=6)).replace(second=0, microsecond=0)


def _set(category, name, reps=8, grams=80000.0):
    return {
        "exercises": [{"category": category, "name": name, "probability": 100.0}],
        "repetitionCount": reps,
        "weight": grams,
        "duration": 40.0,
        "setType": "ACTIVE",
    }


def _activity(activity_id, start=STARTED, minutes=60):
    return {
        "activityId": activity_id,
        "activityName": "Push Day",
        "activityType": {"typeKey": "strength_training"},
        "startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": minutes * 60.0,
    }


class FakeGarmin:
    def __init__(self, activities, sets):
        self.activities = activities
        self.sets = sets

    def get_activities_by_date(self, start, end):
        return copy.deepcopy(self.activities)

    def get_activity_exercise_sets(self, activity_id):
        return {"exerciseSets": copy.deepcopy(self.sets.get(str(activity_id), []))}


class FakeHevy:
    def __init__(self, workouts=()):
        self.workouts = list(workouts)
        self.created = []

    def iter_exercise_templates(self):
        return iter(TEMPLATES)

    def iter_workouts(self):
        return iter(self.workouts)

    def create_workout(self, payload):
        self.created.append(payload)
        return {"workout": [{"id": f"hevy-{len(self.created)}"}]}


@pytest.fixture()
def conn(tmp_path):
    with state.connect(tmp_path / "state.db") as connection:
        yield connection


@pytest.fixture(autouse=True)
def no_hevy2garmin(monkeypatch):
    marked = []
    monkeypatch.setattr(flows.h2g, "mark_synced", lambda h, g: marked.append((h, g)) or True)
    return marked


def _settings(**extra):
    return Settings(hevy_api_key="k", **extra)


def _run(garmin, hevy, conn, now, **extra):
    return flow_b_garmin_to_hevy(garmin, hevy, conn, _settings(**extra), now=now)


BENCH = {"1": [_set("BENCH_PRESS", "BARBELL_BENCH_PRESS")] * 3}


class TestFlowB:
    def test_imports_a_watch_only_session(self, conn, no_hevy2garmin):
        hevy = FakeHevy()
        counters = _run(FakeGarmin([_activity(1)], BENCH), hevy, conn, STARTED + timedelta(hours=5))
        assert counters["imported"] == 1
        payload = hevy.created[0]
        assert payload["exercises"][0]["exercise_template_id"] == "T_BENCH_BB"
        assert len(payload["exercises"][0]["sets"]) == 3
        assert payload["is_private"] is False
        assert state.already_handled(conn, "1")
        assert no_hevy2garmin == [("hevy-1", "1")]

    def test_waits_for_the_import_delay_after_the_session_ends(self, conn):
        hevy = FakeHevy()
        garmin = FakeGarmin([_activity(1, minutes=60)], BENCH)
        # Ended an hour after it started; with the default 180 minute delay it
        # is ready four hours after the start.
        counters = _run(garmin, hevy, conn, STARTED + timedelta(minutes=239))
        assert counters["deferred"] == 1
        assert hevy.created == []
        # Deferred is not a terminal state: the next run looks again.
        assert not state.already_handled(conn, "1")
        counters = _run(garmin, hevy, conn, STARTED + timedelta(minutes=241))
        assert counters["imported"] == 1

    def test_hevy_workout_started_late_in_the_session_is_the_same_session(self, conn):
        """Started 50 minutes into the watch session: outside the start window,
        but the time ranges intersect, so it is the same gym visit."""
        hevy = FakeHevy(
            [
                {
                    "start_time": (STARTED + timedelta(minutes=50)).isoformat(),
                    "end_time": (STARTED + timedelta(minutes=60)).isoformat(),
                }
            ]
        )
        counters = _run(FakeGarmin([_activity(1)], BENCH), hevy, conn, STARTED + timedelta(hours=5))
        assert counters["skipped"] == 1 and hevy.created == []

    def test_a_hevy_workout_saved_during_the_delay_wins(self, conn):
        garmin = FakeGarmin([_activity(1)], BENCH)
        hevy = FakeHevy()
        _run(garmin, hevy, conn, STARTED + timedelta(minutes=70))
        hevy.workouts = [{"start_time": (STARTED + timedelta(minutes=2)).isoformat()}]
        counters = _run(garmin, hevy, conn, STARTED + timedelta(hours=5))
        assert counters["skipped"] == 1
        assert hevy.created == []

    def test_zero_delay_imports_immediately(self, conn):
        hevy = FakeHevy()
        garmin = FakeGarmin([_activity(1)], BENCH)
        counters = _run(garmin, hevy, conn, STARTED + timedelta(minutes=61), import_delay_minutes=0)
        assert counters["imported"] == 1

    def test_private_setting_reaches_the_payload(self, conn):
        hevy = FakeHevy()
        _run(
            FakeGarmin([_activity(1)], BENCH),
            hevy,
            conn,
            STARTED + timedelta(hours=5),
            import_private=True,
        )
        assert hevy.created[0]["is_private"] is True

    def test_dry_run_writes_nothing(self, conn, no_hevy2garmin):
        hevy = FakeHevy()
        counters = _run(
            FakeGarmin([_activity(1)], BENCH),
            hevy,
            conn,
            STARTED + timedelta(hours=5),
            dry_run=True,
        )
        assert counters["imported"] == 1
        assert hevy.created == [] and no_hevy2garmin == []
        assert not state.already_handled(conn, "1")

    def test_activity_paired_by_flow_a_is_skipped(self, conn, monkeypatch):
        monkeypatch.setattr(flows, "claimed_garmin_activity_ids", lambda: {"1"})
        hevy = FakeHevy()
        counters = _run(FakeGarmin([_activity(1)], BENCH), hevy, conn, STARTED + timedelta(hours=5))
        assert counters["skipped"] == 1 and hevy.created == []

    def test_second_run_does_not_reimport(self, conn):
        hevy = FakeHevy()
        garmin = FakeGarmin([_activity(1)], BENCH)
        _run(garmin, hevy, conn, STARTED + timedelta(hours=5))
        _run(garmin, hevy, conn, STARTED + timedelta(hours=4))
        assert len(hevy.created) == 1


class TestFirstReadingPerDay:
    def test_earliest_reading_of_each_day_wins(self):
        entries = [
            {"calendarDate": "2026-08-13", "date": 1755090000000, "weight": 81000},
            {"calendarDate": "2026-08-13", "date": 1755060000000, "weight": 80000},
            {"calendarDate": "2026-08-12", "date": 1754980000000, "weight": 80500},
        ]
        chosen = first_reading_per_day(entries)
        assert [e["weight"] for e in chosen] == [80500, 80000]

    def test_undated_entries_pass_through_for_counting(self):
        assert first_reading_per_day([{"weight": 1}]) == [{"weight": 1}]
