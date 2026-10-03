"""SQLite ledger for the flows this repo owns (B, D and E).

Flows A and C keep their own ledger inside ``~/.hevy2garmin/``; this database
only tracks what we push *into* Hevy and which Garmin activities flow E has
repaired, so a crashed run retries instead of duplicating.

The connection runs in autocommit mode on purpose. A ledger row describes a
write that has already happened on a remote service; holding it in a
transaction until the end of the run means a crash halfway through forgets
workouts that already exist in Hevy, and the next run creates them again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS garmin_to_hevy (
    garmin_activity_id TEXT PRIMARY KEY,
    hevy_workout_id    TEXT,
    status             TEXT NOT NULL,
    note               TEXT,
    synced_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS body_measurements (
    measured_on TEXT PRIMARY KEY,
    synced_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_names_fixed (
    garmin_activity_id TEXT PRIMARY KEY,
    fixed_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    summary    TEXT,
    ok         INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# status values written to garmin_to_hevy.status
IMPORTED = "imported"  # created a Hevy workout from this Garmin activity
SKIPPED = "skipped"  # deliberately not imported; note says why
FAILED = "failed"  # attempted and errored; retried on the next run

# Run history is only read for `status` and debugging. Two runs an hour adds up
# to ~17,000 rows a year, so keep a quarter and drop the rest.
RUN_HISTORY_DAYS = 90


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = db_path or paths().state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "ok" not in columns:  # added in 0.2
        conn.execute("ALTER TABLE runs ADD COLUMN ok INTEGER")


def already_handled(conn: sqlite3.Connection, garmin_activity_id: str) -> bool:
    """True if this activity reached a terminal state. Failures retry."""
    row = conn.execute(
        "SELECT status FROM garmin_to_hevy WHERE garmin_activity_id = ?",
        (str(garmin_activity_id),),
    ).fetchone()
    return row is not None and row["status"] in (IMPORTED, SKIPPED)


def record(
    conn: sqlite3.Connection,
    garmin_activity_id: str,
    status: str,
    hevy_workout_id: str | None = None,
    note: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO garmin_to_hevy
            (garmin_activity_id, hevy_workout_id, status, note, synced_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(garmin_activity_id) DO UPDATE SET
            hevy_workout_id = excluded.hevy_workout_id,
            status          = excluded.status,
            note            = excluded.note,
            synced_at       = excluded.synced_at
        """,
        (str(garmin_activity_id), hevy_workout_id, status, note, _now()),
    )


def body_measurement_synced(conn: sqlite3.Connection, measured_on: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM body_measurements WHERE measured_on = ?", (measured_on,)
    ).fetchone()
    return row is not None


def record_body_measurement(conn: sqlite3.Connection, measured_on: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO body_measurements (measured_on, synced_at) VALUES (?, ?)",
        (measured_on, _now()),
    )


def exercise_names_fixed(conn: sqlite3.Connection, garmin_activity_id: str) -> bool:
    """True if flow E has already confirmed this activity's names render."""
    row = conn.execute(
        "SELECT 1 FROM exercise_names_fixed WHERE garmin_activity_id = ?",
        (str(garmin_activity_id),),
    ).fetchone()
    return row is not None


def record_exercise_names_fixed(conn: sqlite3.Connection, garmin_activity_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO exercise_names_fixed (garmin_activity_id, fixed_at) VALUES (?, ?)",
        (str(garmin_activity_id), _now()),
    )


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, summary: str, ok: bool = True) -> None:
    conn.execute(
        "UPDATE runs SET ended_at = ?, summary = ?, ok = ? WHERE id = ?",
        (_now(), summary, int(ok), run_id),
    )


def prune_runs(conn: sqlite3.Connection, keep_days: int = RUN_HISTORY_DAYS) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
    cur = conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
    return cur.rowcount


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT started_at, ended_at, summary, ok FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
