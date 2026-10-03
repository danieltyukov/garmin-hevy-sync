"""Lock, notifications, ledger durability and the Hevy client's retries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests

from gh_sync import hevy, notify, state
from gh_sync.lock import AlreadyRunning, run_lock


@pytest.fixture()
def conn(tmp_path):
    with state.connect(tmp_path / "state.db") as connection:
        yield connection


class TestRunLock:
    def test_second_holder_is_refused(self, tmp_path):
        lock = tmp_path / "sync.lock"
        with run_lock(lock), pytest.raises(AlreadyRunning), run_lock(lock):
            pass

    def test_released_on_exit(self, tmp_path):
        lock = tmp_path / "sync.lock"
        with run_lock(lock):
            pass
        with run_lock(lock):
            pass

    def test_released_when_the_body_raises(self, tmp_path):
        lock = tmp_path / "sync.lock"
        with pytest.raises(ValueError), run_lock(lock):
            raise ValueError("boom")
        with run_lock(lock):
            pass


class TestLedgerDurability:
    def test_a_write_survives_a_crash_before_the_run_ends(self, tmp_path):
        """Rows describe writes already made on Hevy; they must not wait for a commit."""
        db = tmp_path / "state.db"
        with pytest.raises(RuntimeError), state.connect(db) as conn:
            state.record(conn, "123", state.IMPORTED, hevy_workout_id="w1")
            raise RuntimeError("flow D blew up later in the run")
        with state.connect(db) as conn:
            assert state.already_handled(conn, "123")

    def test_old_runs_table_gains_the_ok_column(self, tmp_path):
        import sqlite3

        db = tmp_path / "state.db"
        legacy = sqlite3.connect(db)
        legacy.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,"
            " ended_at TEXT, summary TEXT)"
        )
        legacy.commit()
        legacy.close()
        with state.connect(db) as conn:
            run_id = state.start_run(conn)
            state.finish_run(conn, run_id, "{}", ok=False)
            assert state.last_run(conn)["ok"] == 0

    def test_prune_keeps_recent_runs(self, conn):
        old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        conn.execute("INSERT INTO runs (started_at) VALUES (?)", (old,))
        state.start_run(conn)
        assert state.prune_runs(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    def test_meta_roundtrip(self, conn):
        state.set_meta(conn, "k", "v")
        assert state.get_meta(conn, "k") == "v"
        state.set_meta(conn, "k", None)
        assert state.get_meta(conn, "k") is None


class TestNotify:
    @pytest.fixture()
    def sent(self, monkeypatch):
        messages: list[tuple[str, str]] = []
        monkeypatch.setattr(notify, "desktop", lambda t, m: messages.append(("desktop", m)) or True)
        monkeypatch.setattr(notify, "push", lambda url, t, m: messages.append(("push", m)) or True)
        return messages

    def test_first_failure_notifies(self, conn, sent):
        notify.failure(conn, "token expired")
        assert sent == [("desktop", "Sync failed: token expired")]

    def test_same_failure_is_not_repeated_within_a_day(self, conn, sent):
        notify.failure(conn, "token expired")
        notify.failure(conn, "token expired")
        assert len(sent) == 1

    def test_a_different_failure_notifies(self, conn, sent):
        notify.failure(conn, "token expired")
        notify.failure(conn, "Hevy rejected the key")
        assert len(sent) == 2

    def test_repeats_after_a_day(self, conn):
        notify.failure.__globals__["state"].set_meta(
            conn, "failure_notified_at", (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        )
        state.set_meta(conn, "failure_message", "token expired")
        assert notify.should_notify(conn, "token expired")

    def test_recovery_resets(self, conn, sent):
        notify.failure(conn, "token expired")
        notify.recovered(conn)
        notify.failure(conn, "token expired")
        assert len(sent) == 2

    def test_push_url_is_used_when_set(self, conn, sent):
        notify.failure(conn, "token expired", notify_url="https://ntfy.sh/topic")
        assert ("push", "Sync failed: token expired") in sent


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class ScriptedSession:
    """Plays back a list of responses (or exceptions) in order."""

    def __init__(self, script):
        self.script = list(script)
        self.headers = {}
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture()
def no_sleep(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(hevy.time, "sleep", waits.append)
    return waits


def _client(script):
    client = hevy.HevyClient("key")
    client.session = ScriptedSession(script)
    return client


class TestHevyClient:
    def test_dropped_connection_is_retried(self, no_sleep):
        client = _client([requests.ConnectionError("reset"), FakeResponse(200, {"ok": 1})])
        assert client._get("/v1/user/info") == {"ok": 1}
        assert no_sleep == [1]

    def test_gives_up_with_a_hevy_error(self, no_sleep):
        client = _client([requests.Timeout("slow")] * hevy.MAX_ATTEMPTS)
        with pytest.raises(hevy.HevyError):
            client._get("/v1/user/info")

    def test_retry_after_is_honoured(self, no_sleep):
        client = _client(
            [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {"ok": 1})]
        )
        client._get("/v1/workouts")
        assert no_sleep == [7.0]

    def test_errors_carry_the_status(self, no_sleep):
        client = _client([FakeResponse(401, {"error": "bad key"})])
        with pytest.raises(hevy.HevyError) as caught:
            client._get("/v1/user/info")
        assert caught.value.status == 401

    def test_pagination_halves_page_size_on_400(self, no_sleep):
        client = _client(
            [
                FakeResponse(400, {"error": "pageSize too large"}),
                FakeResponse(200, {"workouts": [{"id": 1}], "page_count": 1}),
            ]
        )
        assert list(client.iter_workouts(page_size=10)) == [{"id": 1}]
        assert client.session.calls[1][2]["pageSize"] == 5

    def test_a_400_mentioning_400_elsewhere_is_not_confused(self, no_sleep):
        """The old check searched the message text for "400"."""
        client = _client([FakeResponse(404, {"error": "workout 4004000 not found"})])
        with pytest.raises(hevy.HevyError):
            list(client.iter_workouts())


class TestHevyWritesAreNotDuplicated:
    """A POST that may have reached Hevy must never be sent twice."""

    def test_read_timeout_on_post_is_not_retried(self, no_sleep):
        client = _client([requests.ReadTimeout("no answer"), FakeResponse(201, {"id": 1})])
        with pytest.raises(hevy.HevyError):
            client.create_workout({"title": "x"})
        assert len(client.session.calls) == 1

    def test_server_error_on_post_is_not_retried(self, no_sleep):
        client = _client([FakeResponse(502), FakeResponse(201, {"id": 1})])
        with pytest.raises(hevy.HevyError):
            client.create_workout({"title": "x"})
        assert len(client.session.calls) == 1

    def test_connect_timeout_on_post_is_retried(self, no_sleep):
        """The connection was never made, so nothing reached Hevy."""
        client = _client(
            [requests.ConnectTimeout("no route"), FakeResponse(201, {"workout": [{"id": "w"}]})]
        )
        assert hevy.extract_workout_id(client.create_workout({"title": "x"})) == "w"

    def test_rate_limited_post_is_retried(self, no_sleep):
        client = _client([FakeResponse(429), FakeResponse(201, {"workout": [{"id": "w"}]})])
        assert hevy.extract_workout_id(client.create_workout({"title": "x"})) == "w"

    def test_reads_still_retry_server_errors(self, no_sleep):
        client = _client([FakeResponse(503), FakeResponse(200, {"ok": 1})])
        assert client._get("/v1/workouts") == {"ok": 1}
