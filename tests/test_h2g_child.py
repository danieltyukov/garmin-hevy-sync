"""The hevy2garmin child entry point and its fix for the Garmin workout listing."""

from __future__ import annotations

import sys

import pytest
from hevy2garmin import garmin, sync

from gh_sync import h2g_child
from gh_sync.config import hevy2garmin_command

UPSTREAM_LIST_WORKOUTS = garmin.list_workouts


class _Session:
    """Stands in for garminconnect's client, which refuses a query string in the path."""

    def __init__(self, body):
        self.body = body
        self.calls = []

    def request(self, method, domain, path, **kwargs):
        if "?" in path:
            raise ValueError(f"Invalid API path: {path!r}")
        self.calls.append((method, path, kwargs))
        return self.body


class _Garmin:
    def __init__(self, body):
        self.client = _Session(body)


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)


def test_upstream_listing_still_needs_the_fix():
    # Once this fails, hevy2garmin lists workouts correctly itself and h2g_child can go.
    with pytest.raises(ValueError, match="Invalid API path"):
        UPSTREAM_LIST_WORKOUTS(_Garmin([]), limit=999)


def test_listing_sends_the_query_as_params():
    client = _Garmin([{"workoutId": 1}])
    assert h2g_child.list_workouts(client, limit=999) == [{"workoutId": 1}]
    assert client.client.calls == [
        (
            "GET",
            "/workout-service/workouts",
            {"params": {"start": 1, "limit": 999, "myWorkoutsOnly": "true"}},
        )
    ]


def test_listing_that_is_not_a_list_is_unknown_not_empty():
    with pytest.raises(RuntimeError, match="not a list"):
        h2g_child.list_workouts(_Garmin({"message": "error"}))


def test_patch_also_replaces_the_name_sync_imported(monkeypatch):
    monkeypatch.setattr(garmin, "list_workouts", garmin.list_workouts)
    monkeypatch.setattr(sync, "list_workouts", sync.list_workouts)
    h2g_child.patch()
    assert garmin.list_workouts is h2g_child.list_workouts
    assert sync.list_workouts is h2g_child.list_workouts


def test_main_patches_before_handing_over_to_hevy2garmin(monkeypatch):
    seen = []
    monkeypatch.setattr(h2g_child, "patch", lambda: seen.append("patch"))
    monkeypatch.setattr(
        h2g_child.runpy, "run_module", lambda name, **kwargs: seen.append((name, kwargs))
    )
    h2g_child.main()
    assert seen == ["patch", ("hevy2garmin.cli", {"run_name": "__main__", "alter_sys": True})]


def test_child_process_starts_through_the_fix():
    assert hevy2garmin_command() == [sys.executable, "-m", "gh_sync.h2g_child"]
