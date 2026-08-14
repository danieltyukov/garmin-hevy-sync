from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from gh_sync import state
from gh_sync.flows import _overlaps, _parse_hevy_time
from gh_sync.garmin_client import parse_start

NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


class TestParseHevyTime:
    def test_zulu_suffix(self):
        assert _parse_hevy_time("2026-07-29T18:00:00Z") == NOW

    def test_explicit_offset(self):
        assert _parse_hevy_time("2026-07-29T20:00:00+02:00") == NOW

    def test_naive_is_treated_as_utc(self):
        assert _parse_hevy_time("2026-07-29T18:00:00") == NOW

    def test_garbage_returns_none(self):
        assert _parse_hevy_time("not a time") is None
        assert _parse_hevy_time(None) is None


class TestParseGarminStart:
    def test_space_separated_gmt_string_is_utc(self):
        assert parse_start({"startTimeGMT": "2026-07-29 18:00:00"}) == NOW

    def test_iso_with_millis(self):
        assert parse_start({"startTimeGMT": "2026-07-29T18:00:00.0"}) == NOW

    def test_missing_field(self):
        assert parse_start({}) is None

    def test_unparseable_field(self):
        assert parse_start({"startTimeGMT": "yesterday"}) is None


class TestOverlaps:
    def test_exact_match_overlaps(self):
        assert _overlaps(NOW, [NOW], 45)

    def test_inside_window(self):
        assert _overlaps(NOW, [NOW + timedelta(minutes=30)], 45)

    def test_on_the_boundary_counts(self):
        assert _overlaps(NOW, [NOW + timedelta(minutes=45)], 45)

    def test_outside_window(self):
        assert not _overlaps(NOW, [NOW + timedelta(minutes=46)], 45)

    def test_earlier_side_of_the_window(self):
        assert _overlaps(NOW, [NOW - timedelta(minutes=44)], 45)

    def test_empty_candidate_list(self):
        assert not _overlaps(NOW, [], 45)

    def test_picks_any_match_among_many(self):
        others = [NOW - timedelta(days=3), NOW + timedelta(minutes=5), NOW + timedelta(days=1)]
        assert _overlaps(NOW, others, 45)


@pytest.fixture()
def conn(tmp_path):
    with state.connect(tmp_path / "state.db") as connection:
        yield connection


class TestLedger:
    def test_unknown_activity_is_not_handled(self, conn):
        assert not state.already_handled(conn, "123")

    def test_imported_is_terminal(self, conn):
        state.record(conn, "123", state.IMPORTED, hevy_workout_id="w1")
        assert state.already_handled(conn, "123")

    def test_skipped_is_terminal(self, conn):
        state.record(conn, "123", state.SKIPPED, note="already in Hevy")
        assert state.already_handled(conn, "123")

    def test_failed_is_retried(self, conn):
        state.record(conn, "123", state.FAILED, note="boom")
        assert not state.already_handled(conn, "123")

    def test_record_is_idempotent_on_activity_id(self, conn):
        state.record(conn, "123", state.FAILED, note="boom")
        state.record(conn, "123", state.IMPORTED, hevy_workout_id="w1")
        rows = conn.execute("SELECT * FROM garmin_to_hevy").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == state.IMPORTED
        assert rows[0]["hevy_workout_id"] == "w1"

    def test_integer_and_string_ids_are_the_same_row(self, conn):
        state.record(conn, 123, state.IMPORTED, hevy_workout_id="w1")
        assert state.already_handled(conn, "123")

    def test_body_measurement_ledger(self, conn):
        assert not state.body_measurement_synced(conn, "2026-07-29")
        state.record_body_measurement(conn, "2026-07-29")
        assert state.body_measurement_synced(conn, "2026-07-29")

    def test_run_lifecycle(self, conn):
        run_id = state.start_run(conn)
        state.finish_run(conn, run_id, '{"ok": true}')
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["ended_at"] is not None
        assert row["summary"] == '{"ok": true}'


class TestExtractWorkoutId:
    """The live API wraps the created workout in a list, not an object."""

    def test_list_under_workout_key(self):
        from gh_sync.hevy import extract_workout_id

        assert extract_workout_id({"workout": [{"id": "abc"}]}) == "abc"

    def test_object_under_workout_key(self):
        from gh_sync.hevy import extract_workout_id

        assert extract_workout_id({"workout": {"id": "abc"}}) == "abc"

    def test_bare_object(self):
        from gh_sync.hevy import extract_workout_id

        assert extract_workout_id({"id": "abc"}) == "abc"

    def test_bare_list(self):
        from gh_sync.hevy import extract_workout_id

        assert extract_workout_id([{"id": "abc"}]) == "abc"

    def test_non_string_id_is_stringified(self):
        from gh_sync.hevy import extract_workout_id

        assert extract_workout_id({"workout": [{"id": 12345}]}) == "12345"

    def test_empty_and_malformed_shapes_return_none(self):
        from gh_sync.hevy import extract_workout_id

        for payload in ({}, {"workout": []}, {"workout": [{}]}, [], None, "nope"):
            assert extract_workout_id(payload) is None


class FakeGarminScale:
    """Records the window it was asked for so the caller can assert on it."""

    def __init__(self, entries):
        self.entries = entries
        self.asked_start = None
        self.asked_end = None

    def get_body_composition(self, start, end):
        self.asked_start, self.asked_end = start, end
        return {"dateWeightList": self.entries}

    @property
    def window_days(self):
        return (date.fromisoformat(self.asked_end) - date.fromisoformat(self.asked_start)).days


class FakeHevyScale:
    def __init__(self, conflict_dates=()):
        self.posted = []
        self.conflict_dates = set(conflict_dates)

    def create_body_measurement(self, measurement):
        self.posted.append(measurement)
        if measurement["date"] in self.conflict_dates:
            return None  # Hevy already had that date
        return {"id": 1}


def _settings(**overrides):
    from gh_sync.config import Settings

    return Settings(
        hevy_api_key="k", garmin_email="e", garmin_password="p", **overrides
    )


class TestFlowDBodyMeasurements:
    """Weigh-ins are sparse, so the window must not be flow B's fortnight."""

    def test_uses_the_body_window_not_the_workout_window(self, conn):
        from gh_sync.flows import flow_d_body_measurements

        garmin = FakeGarminScale([])
        flow_d_body_measurements(garmin, FakeHevyScale(), conn, _settings())
        assert garmin.window_days == 365
        assert garmin.window_days != 14

    def test_window_is_configurable(self, conn):
        from gh_sync.flows import flow_d_body_measurements

        garmin = FakeGarminScale([])
        flow_d_body_measurements(
            garmin, FakeHevyScale(), conn, _settings(body_lookback_days=30)
        )
        assert garmin.window_days == 30

    def test_grams_are_converted_to_kilograms(self, conn):
        from gh_sync.flows import flow_d_body_measurements

        hevy = FakeHevyScale()
        entries = [{"calendarDate": "2026-03-14", "weight": 72500.0, "muscleMass": 30500.0}]
        counters = flow_d_body_measurements(FakeGarminScale(entries), hevy, conn, _settings())
        assert counters["synced"] == 1
        assert hevy.posted == [
            {"date": "2026-03-14", "weight_kg": 72.5, "lean_mass_kg": 30.5}
        ]

    def test_entry_without_a_date_is_counted_not_dropped(self, conn):
        """An all-zero summary must not be able to hide unreadable entries."""
        from gh_sync.flows import flow_d_body_measurements

        entries = [{"weight": 72500.0}, {"calendarDate": "2026-03-14", "weight": 72500.0}]
        counters = flow_d_body_measurements(
            FakeGarminScale(entries), FakeHevyScale(), conn, _settings()
        )
        assert counters["no_date"] == 1
        assert counters["considered"] == 2
        assert counters["synced"] == 1

    def test_no_weigh_ins_is_distinguishable_from_unreadable_ones(self, conn):
        from gh_sync.flows import flow_d_body_measurements

        counters = flow_d_body_measurements(
            FakeGarminScale([]), FakeHevyScale(), conn, _settings()
        )
        assert counters["considered"] == 0
        assert counters["no_date"] == 0

    def test_already_synced_date_is_not_posted_again(self, conn):
        from gh_sync.flows import flow_d_body_measurements

        state.record_body_measurement(conn, "2026-03-14")
        hevy = FakeHevyScale()
        entries = [{"calendarDate": "2026-03-14", "weight": 72500.0}]
        counters = flow_d_body_measurements(FakeGarminScale(entries), hevy, conn, _settings())
        assert hevy.posted == []
        assert counters["skipped"] == 1

    def test_date_only_entry_is_skipped(self, conn):
        from gh_sync.flows import flow_d_body_measurements

        hevy = FakeHevyScale()
        entries = [{"calendarDate": "2026-03-14", "weight": None, "bodyFat": None}]
        counters = flow_d_body_measurements(FakeGarminScale(entries), hevy, conn, _settings())
        assert hevy.posted == []
        assert counters["skipped"] == 1

    def test_conflict_on_hevy_side_is_recorded_as_synced_state(self, conn):
        """A 409 means Hevy already has it, so it must not be retried forever."""
        from gh_sync.flows import flow_d_body_measurements

        hevy = FakeHevyScale(conflict_dates={"2026-03-14"})
        entries = [{"calendarDate": "2026-03-14", "weight": 72500.0}]
        counters = flow_d_body_measurements(FakeGarminScale(entries), hevy, conn, _settings())
        assert counters["skipped"] == 1
        assert state.body_measurement_synced(conn, "2026-03-14")


def _active_set(category, name, probability, **extra):
    """One ACTIVE set shaped exactly like the live exerciseSets payload."""
    one = {
        "exercises": [{"category": category, "name": name, "probability": probability}],
        "duration": 48.301,
        "repetitionCount": 15,
        "weight": 0.0,
        "setType": "ACTIVE",
        "startTime": "2026-08-13T15:17:21.0",
        "wktStepIndex": 0,
        "messageIndex": 0,
    }
    one.update(extra)
    return one


def _rest_set():
    return {
        "exercises": [],
        "duration": 144.903,
        "repetitionCount": None,
        "weight": None,
        "setType": "REST",
        "startTime": "2026-08-13T15:18:09.0",
        "wktStepIndex": 0,
        "messageIndex": 1,
    }


def _activity(activity_id, type_key="strength_training"):
    return {
        "activityId": activity_id,
        "activityType": {"typeKey": type_key},
        "startTimeGMT": "2026-08-13 15:17:21",
    }


class FakeGarminGym:
    def __init__(self, activities, sets_by_id):
        self.activities = activities
        self.sets_by_id = sets_by_id
        self.puts = {}
        self.reads = []
        self.put_fails_for = set()

    def get_activities_by_date(self, start, end):
        return self.activities

    def get_activity_exercise_sets(self, activity_id):
        self.reads.append(str(activity_id))
        import copy as _copy

        return _copy.deepcopy(self.sets_by_id[str(activity_id)])

    def set_activity_exercise_sets(self, activity_id, payload):
        if str(activity_id) in self.put_fails_for:
            raise RuntimeError("Garmin rejected the PUT")
        self.puts[str(activity_id)] = payload
        return {}


def _probabilities(payload):
    return [
        e.get("probability")
        for s in payload["exerciseSets"]
        for e in (s.get("exercises") or [])
    ]


class TestFlowEExerciseNames:
    """hevy2garmin pushes valid names with probability 0.0, which Garmin's UI
    treats as "nothing identified" and renders as "Choose an Exercise"."""

    def test_named_exercise_with_zero_probability_is_boosted(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)],
            {"1": {"activityId": 1, "exerciseSets": [
                _active_set("SQUAT", "PISTOL_SQUAT", 0.0), _rest_set()]}},
        )
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert counters["fixed"] == 1
        assert _probabilities(garmin.puts["1"]) == [100.0]

    def test_native_watch_detection_is_never_touched(self, conn):
        """Garmin's own rep detection writes a real confidence; leave it be."""
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)],
            {"1": {"activityId": 1, "exerciseSets": [
                _active_set("SQUAT", "PISTOL_SQUAT", 87.5)]}},
        )
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert garmin.puts == {}
        assert counters["fixed"] == 0

    def test_unknown_category_is_not_given_a_fake_confidence(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)],
            {"1": {"activityId": 1, "exerciseSets": [
                _active_set("UNKNOWN", None, 0.0)]}},
        )
        flow_e_exercise_names(garmin, conn, _settings())
        assert garmin.puts == {}

    def test_missing_category_is_not_given_a_fake_confidence(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)],
            {"1": {"activityId": 1, "exerciseSets": [_active_set(None, None, 0.0)]}},
        )
        flow_e_exercise_names(garmin, conn, _settings())
        assert garmin.puts == {}

    def test_only_the_probability_field_changes(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        original = {"activityId": 1, "exerciseSets": [
            _active_set("SQUAT", "PISTOL_SQUAT", 0.0), _rest_set()]}
        garmin = FakeGarminGym([_activity(1)], {"1": original})
        flow_e_exercise_names(garmin, conn, _settings())

        sent = garmin.puts["1"]
        assert len(sent["exerciseSets"]) == 2
        for before, after in zip(original["exerciseSets"], sent["exerciseSets"]):
            assert {k: v for k, v in after.items() if k != "exercises"} == {
                k: v for k, v in before.items() if k != "exercises"
            }
            for b_ex, a_ex in zip(before["exercises"], after["exercises"]):
                assert a_ex["category"] == b_ex["category"]
                assert a_ex["name"] == b_ex["name"]

    def test_a_repaired_activity_is_not_fetched_again(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        sets = {"1": {"activityId": 1, "exerciseSets": [
            _active_set("SQUAT", "PISTOL_SQUAT", 0.0)]}}
        garmin = FakeGarminGym([_activity(1)], sets)
        flow_e_exercise_names(garmin, conn, _settings())
        sets["1"]["exerciseSets"][0]["exercises"][0]["probability"] = 100.0

        garmin.reads.clear()
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert garmin.reads == []
        assert counters["checked"] == 0

    def test_an_already_correct_activity_is_recorded_so_it_stops_being_fetched(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)],
            {"1": {"activityId": 1, "exerciseSets": [
                _active_set("SQUAT", "PISTOL_SQUAT", 100.0)]}},
        )
        flow_e_exercise_names(garmin, conn, _settings())
        garmin.reads.clear()
        flow_e_exercise_names(garmin, conn, _settings())
        assert garmin.reads == []

    def test_an_activity_with_no_names_yet_is_checked_again_next_run(self, conn):
        """Flow A may not have pushed its sets yet; do not write it off."""
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)], {"1": {"activityId": 1, "exerciseSets": [_rest_set()]}}
        )
        flow_e_exercise_names(garmin, conn, _settings())
        garmin.reads.clear()
        flow_e_exercise_names(garmin, conn, _settings())
        assert garmin.reads == ["1"]

    def test_a_failed_put_is_counted_and_retried_next_run(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1)],
            {"1": {"activityId": 1, "exerciseSets": [
                _active_set("SQUAT", "PISTOL_SQUAT", 0.0)]}},
        )
        garmin.put_fails_for = {"1"}
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert counters["failed"] == 1
        assert counters["fixed"] == 0

        garmin.put_fails_for = set()
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert counters["fixed"] == 1

    def test_each_activity_is_handled_independently(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym(
            [_activity(1), _activity(2)],
            {
                "1": {"activityId": 1, "exerciseSets": [
                    _active_set("SQUAT", "PISTOL_SQUAT", 0.0)]},
                "2": {"activityId": 2, "exerciseSets": [
                    _active_set("PUSH_UP", "PUSH_UP", 0.0)]},
            },
        )
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert counters["fixed"] == 2
        assert set(garmin.puts) == {"1", "2"}

    def test_non_strength_activities_are_ignored(self, conn):
        from gh_sync.flows import flow_e_exercise_names

        garmin = FakeGarminGym([_activity(1, "running")], {})
        counters = flow_e_exercise_names(garmin, conn, _settings())
        assert counters["checked"] == 0
        assert garmin.puts == {}


class TestWriteResponseParsing:
    """A write Hevy has already accepted must never raise on its reply body.

    The measurement is stored before the body is read, so a decode error would
    abort the run after the write and orphan the row in the ledger.
    """

    @staticmethod
    def _response(status, body):
        import requests

        resp = requests.Response()
        resp.status_code = status
        resp._content = body.encode()
        resp.headers["Content-Type"] = "application/json"
        return resp

    def test_empty_success_body_is_not_an_error(self):
        from gh_sync.hevy import _json_or_empty

        assert _json_or_empty(self._response(201, "")) == {}

    def test_non_json_success_body_is_not_an_error(self):
        from gh_sync.hevy import _json_or_empty

        assert _json_or_empty(self._response(200, "Created")) == {}

    def test_json_body_is_returned_intact(self):
        from gh_sync.hevy import _json_or_empty

        assert _json_or_empty(self._response(200, '{"id": 7}')) == {"id": 7}

    def test_empty_body_still_counts_as_created_not_conflict(self):
        """{} must stay distinguishable from the None that means 409."""
        from gh_sync.hevy import _json_or_empty

        assert _json_or_empty(self._response(201, "")) is not None


class TestClaimedGarminActivityIds:
    """Flow B must not re-import an activity flow A already paired."""

    @staticmethod
    def _make_db(path, rows):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE synced_workouts (hevy_id TEXT, garmin_activity_id TEXT)"
        )
        conn.executemany("INSERT INTO synced_workouts VALUES (?, ?)", rows)
        conn.commit()
        conn.close()

    def test_reads_paired_activity_ids(self, tmp_path):
        from gh_sync.flows import claimed_garmin_activity_ids

        db = tmp_path / "sync.db"
        self._make_db(db, [("hevy-1", "23769638795"), ("hevy-2", "999")])
        assert claimed_garmin_activity_ids(db) == {"23769638795", "999"}

    def test_ignores_unpaired_rows(self, tmp_path):
        from gh_sync.flows import claimed_garmin_activity_ids

        db = tmp_path / "sync.db"
        self._make_db(db, [("hevy-1", None), ("hevy-2", ""), ("hevy-3", "42")])
        assert claimed_garmin_activity_ids(db) == {"42"}

    def test_ids_are_strings_regardless_of_storage_type(self, tmp_path):
        from gh_sync.flows import claimed_garmin_activity_ids

        db = tmp_path / "sync.db"
        self._make_db(db, [("hevy-1", 23769638795)])
        assert claimed_garmin_activity_ids(db) == {"23769638795"}

    def test_missing_database_is_not_an_error(self, tmp_path):
        from gh_sync.flows import claimed_garmin_activity_ids

        assert claimed_garmin_activity_ids(tmp_path / "absent.db") == set()

    def test_unreadable_database_falls_back_to_empty(self, tmp_path):
        from gh_sync.flows import claimed_garmin_activity_ids

        db = tmp_path / "sync.db"
        db.write_text("this is not a sqlite database")
        assert claimed_garmin_activity_ids(db) == set()

    def test_schema_without_the_table_falls_back_to_empty(self, tmp_path):
        import sqlite3

        from gh_sync.flows import claimed_garmin_activity_ids

        db = tmp_path / "sync.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE something_else (x TEXT)")
        conn.commit()
        conn.close()
        assert claimed_garmin_activity_ids(db) == set()
