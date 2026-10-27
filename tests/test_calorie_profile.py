"""Keeping hevy2garmin's calorie profile weight in step with the newest weigh-in."""

from __future__ import annotations

import json

import pytest

from gh_sync import calorie_profile, cli
from gh_sync.config import hevy2garmin_home


class _Hevy:
    def __init__(self, measurements=(), error=None):
        self.measurements = list(measurements)
        self.error = error

    def iter_body_measurements(self):
        if self.error:
            raise self.error
        return iter(self.measurements)


@pytest.fixture()
def h2g_config():
    path = hevy2garmin_home() / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hevy_api_key": "k",
                "user_profile": {"weight_kg": 63.0, "birth_year": 2003, "vo2max": 45.0},
            }
        )
    )
    return path


def _profile(path):
    return json.loads(path.read_text())["user_profile"]


def test_newest_weigh_in_replaces_the_profile_weight(h2g_config):
    hevy = _Hevy(
        [
            {"date": "2026-08-22", "weight_kg": 70.03},
            {"date": "2026-10-27", "weight_kg": 72},
            {"date": "2026-10-28", "fat_percent": 15.0},
        ]
    )
    assert calorie_profile.refresh_weight(hevy) == 72.0
    assert _profile(h2g_config) == {"weight_kg": 72.0, "birth_year": 2003, "vo2max": 45.0}
    assert json.loads(h2g_config.read_text())["hevy_api_key"] == "k"


def test_unchanged_weight_leaves_the_file_alone(h2g_config):
    before = h2g_config.read_text()
    assert calorie_profile.refresh_weight(_Hevy([{"date": "2026-05-25", "weight_kg": 63}])) is None
    assert h2g_config.read_text() == before


def test_no_weigh_ins_leaves_the_profile_alone(h2g_config):
    assert calorie_profile.refresh_weight(_Hevy([])) is None
    assert _profile(h2g_config)["weight_kg"] == 63.0


def test_dry_run_changes_nothing(h2g_config):
    hevy = _Hevy([{"date": "2026-10-27", "weight_kg": 72}])
    assert calorie_profile.refresh_weight(hevy, dry_run=True) is None
    assert _profile(h2g_config)["weight_kg"] == 63.0


def test_without_a_hevy2garmin_config_there_is_nothing_to_update():
    assert calorie_profile.refresh_weight(_Hevy([{"date": "2026-10-27", "weight_kg": 72}])) is None
    assert not (hevy2garmin_home() / "config.json").exists()


def test_hevy_being_down_does_not_cost_the_run(h2g_config, caplog):
    assert calorie_profile.refresh_weight(_Hevy(error=RuntimeError("Hevy is down"))) is None
    assert _profile(h2g_config)["weight_kg"] == 63.0
    assert "Hevy is down" in caplog.text


class TestSyncOrder:
    @pytest.fixture()
    def order(self, monkeypatch):
        monkeypatch.setenv("HEVY_API_KEY", "k")
        seen = []
        monkeypatch.setattr(
            cli, "refresh_weight", lambda hevy, dry_run=False: seen.append("weight")
        )
        monkeypatch.setattr(
            cli.h2g, "run_logged", lambda args, dry_run=False: seen.append(args[0]) or True
        )
        return seen

    def test_weight_is_refreshed_before_hevy2garmin_writes_calories(self, order):
        assert cli.main(["sync", "--flows", "a", "c"]) == 0
        assert order == ["weight", "sync", "sync-routines"]

    def test_weight_is_left_alone_when_flow_a_does_not_run(self, order):
        assert cli.main(["sync", "--flows", "c"]) == 0
        assert order == ["sync-routines"]
