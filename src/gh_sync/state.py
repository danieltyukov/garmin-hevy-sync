"""SQLite ledger for the flows this repo owns (B and D).

Flow A and C keep their own ledger inside ``~/.hevy2garmin/``; this database
only tracks what we push *into* Hevy, so a crashed run retries instead of
duplicating. Every write is idempotent on the Garmin-side id.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import STATE_DB

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
    summary    TEXT
);
"""

# status values written to garmin_to_hevy.status
IMPORTED = "imported"  # created a Hevy workout from this Garmin activity
SKIPPED = "skipped"  # deliberately not imported; note says why
FAILED = "failed"  # attempted and errored; retried on the next run


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: Path = STATE_DB) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


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
        "INSERT OR REPLACE INTO exercise_names_fixed (garmin_activity_id, fixed_at) "
        "VALUES (?, ?)",
        (str(garmin_activity_id), _now()),
    )


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, summary: str) -> None:
    conn.execute(
        "UPDATE runs SET ended_at = ?, summary = ? WHERE id = ?",
        (_now(), summary, run_id),
    )
