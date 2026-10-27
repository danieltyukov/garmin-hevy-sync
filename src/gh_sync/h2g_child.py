"""Entry point for the hevy2garmin child process.

hevy2garmin 0.12.0 lists Garmin workouts with the query string inside the URL
path, and garminconnect 0.3.17 rejects that path as invalid. Every routine sync
then logged "Could not list Garmin workouts" and deduplicated against its local
database alone, so a reset database would create every routine on Garmin again.
This module fixes the listing and then runs the hevy2garmin CLI unchanged, still
as a child process.
"""

from __future__ import annotations

import runpy
import time
from typing import Any


def list_workouts(client: Any, limit: int = 100) -> list[dict]:
    """hevy2garmin's ``list_workouts`` with the query passed as request params."""
    time.sleep(1.0)  # hevy2garmin's own manual rate limit
    resp = client.client.request(
        "GET",
        "connectapi",
        "/workout-service/workouts",
        params={"start": 1, "limit": limit, "myWorkoutsOnly": "true"},
    )
    data = resp.json() if hasattr(resp, "json") else resp
    # An error envelope must not read as an empty library, or reconciliation
    # would treat every synced routine as deleted on Garmin.
    if not isinstance(data, list):
        raise RuntimeError(
            f"Garmin workout listing returned a {type(data).__name__}, not a list; "
            "treating as unknown rather than empty"
        )
    return data


def patch() -> None:
    from hevy2garmin import garmin, sync

    garmin.list_workouts = list_workouts
    # sync.py imported the function by name, so its reference is replaced too.
    sync.list_workouts = list_workouts


def main() -> None:
    patch()
    runpy.run_module("hevy2garmin.cli", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
