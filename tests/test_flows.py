from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
