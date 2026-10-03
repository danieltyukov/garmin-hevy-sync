"""The command line: argument handling, the sync orchestrator and setup's config writer."""

from __future__ import annotations

import json

import pytest

from gh_sync import cli, notify, state
from gh_sync.config import paths
from gh_sync.garmin_client import GarminLoginRequired
from gh_sync.setup_wizard import DEFAULT_PROFILE, apply_hevy2garmin_config


def test_version(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0
    assert "garmin-hevy-sync" in capsys.readouterr().out


def test_status_before_any_run(capsys):
    assert cli.main(["status"]) == 0
    assert "No sync has run yet" in capsys.readouterr().out


def test_sync_without_a_key_explains_what_to_do(capsys):
    assert cli.main(["sync"]) == 2
    assert "garmin-hevy-sync setup" in capsys.readouterr().err


def test_home_flag(tmp_path, capsys):
    target = tmp_path / "elsewhere"
    assert cli.main(["--home", str(target), "status"]) == 0
    assert str(target) in capsys.readouterr().out


def test_schedule_dry_run_prints_the_plan(monkeypatch, capsys):
    monkeypatch.setattr(cli.schedule, "detect_backend", lambda: "cron")
    assert cli.main(["schedule", "install", "--dry-run", "--every", "1h"]) == 0
    assert "0 */1 * * *" in capsys.readouterr().out


def test_logs_tail(capsys):
    cli.main(["status"])  # creates the log file
    capsys.readouterr()
    paths().log_file.write_text("".join(f"line {i}\n" for i in range(100)), encoding="utf-8")
    assert cli.main(["logs", "-n", "3"]) == 0
    assert capsys.readouterr().out.splitlines() == ["line 97", "line 98", "line 99"]


class TestSyncOrchestration:
    @pytest.fixture()
    def wired(self, monkeypatch):
        monkeypatch.setenv("HEVY_API_KEY", "k")
        calls = {"h2g": [], "notified": [], "recovered": 0}
        monkeypatch.setattr(
            cli.h2g, "run_logged", lambda args, dry_run=False: calls["h2g"].append(args) or True
        )
        monkeypatch.setattr(
            notify, "failure", lambda conn, message, url="": calls["notified"].append(message)
        )

        def recovered(conn):
            calls["recovered"] += 1

        monkeypatch.setattr(notify, "recovered", recovered)
        return calls

    def test_expired_garmin_sign_in_fails_the_run_and_notifies(self, monkeypatch, wired):
        def expired():
            raise GarminLoginRequired("Garmin sign-in needed. Run `garmin-hevy-sync login`.")

        monkeypatch.setattr(cli, "resume", expired)
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        assert cli.main(["sync"]) == 1
        assert wired["h2g"] == [["sync"], ["sync-routines"]]
        assert wired["notified"] and "sign-in" in wired["notified"][0]
        with state.connect() as conn:
            assert state.last_run(conn)["ok"] == 0

    def test_one_flow_failing_does_not_stop_the_others(self, monkeypatch, wired):
        monkeypatch.setattr(cli, "resume", lambda: object())
        monkeypatch.setattr(cli, "_interactive", lambda: False)

        def broken(*_args, **_kwargs):
            raise RuntimeError("Hevy is down")

        ran = []
        monkeypatch.setattr(cli, "flow_b_garmin_to_hevy", broken)
        monkeypatch.setattr(
            cli, "flow_d_body_measurements", lambda *a, **k: ran.append("d") or {"synced": 0}
        )
        monkeypatch.setattr(
            cli, "flow_e_exercise_names", lambda *a, **k: ran.append("e") or {"fixed": 0}
        )
        assert cli.main(["sync"]) == 1
        assert ran == ["d", "e"]
        with state.connect() as conn:
            summary = json.loads(state.last_run(conn)["summary"])
        assert summary["b_garmin_to_hevy"].startswith("failed")

    def test_a_clean_background_run_clears_earlier_failures(self, monkeypatch, wired):
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        assert cli.main(["sync", "--flows", "a"]) == 0
        assert wired["recovered"] == 1

    def test_interactive_runs_do_not_notify(self, monkeypatch, wired):
        monkeypatch.setattr(cli, "resume", lambda: (_ for _ in ()).throw(GarminLoginRequired("x")))
        monkeypatch.setattr(cli, "_interactive", lambda: True)
        assert cli.main(["sync"]) == 1
        assert wired["notified"] == []

    def test_dry_run_is_passed_to_hevy2garmin(self, monkeypatch):
        monkeypatch.setenv("HEVY_API_KEY", "k")
        seen = []
        monkeypatch.setattr(
            cli.h2g, "run_logged", lambda args, dry_run=False: seen.append(dry_run) or True
        )
        assert cli.main(["sync", "--flows", "a", "c", "--dry-run"]) == 0
        assert seen == [True, True]


class TestApplyHevy2GarminConfig:
    def test_removes_a_stored_password_and_keeps_other_settings(self, tmp_path):
        from gh_sync.config import hevy2garmin_home

        target = hevy2garmin_home() / "config.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "garmin_password": "hunter2",
                    "timing": {"working_set_seconds": 33},
                    "sync": {"default_limit": 5},
                }
            )
        )
        notes = apply_hevy2garmin_config(DEFAULT_PROFILE, "api-key", "me@example.com")
        written = json.loads(target.read_text())
        assert "garmin_password" not in written
        assert written["hevy_api_key"] == "api-key"
        assert written["garmin_email"] == "me@example.com"
        assert written["timing"]["working_set_seconds"] == 33
        assert written["sync"]["default_limit"] == 5
        assert written["sync"]["grace_period_minutes"] == 120
        assert written["merge_watch_strategy"] == "merge"
        assert "_comment" not in written
        assert any("password" in note for note in notes)

    def test_does_not_pick_the_password_up_from_the_environment(self, monkeypatch):
        from gh_sync.config import hevy2garmin_home

        monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
        apply_hevy2garmin_config(DEFAULT_PROFILE, "k", "e")
        assert "hunter2" not in (hevy2garmin_home() / "config.json").read_text()


def test_hevy2garmin_child_never_gets_the_password(monkeypatch):
    from gh_sync import h2g

    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    assert "GARMIN_PASSWORD" not in h2g.child_env()


class TestBackgroundFailuresLeaveATrace:
    def test_missing_key_in_a_scheduled_run_is_recorded_and_notified(self, monkeypatch):
        notified = []
        monkeypatch.setattr(
            notify, "failure", lambda conn, message, url="": notified.append(message)
        )
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        assert cli.main(["sync"]) == 2
        assert notified and "Hevy API key" in notified[0]
        with state.connect() as conn:
            assert state.last_run(conn)["ok"] == 0
        assert "Hevy API key" in paths().log_file.read_text(encoding="utf-8")

    def test_interactive_config_error_is_not_notified(self, monkeypatch):
        notified = []
        monkeypatch.setattr(
            notify, "failure", lambda conn, message, url="": notified.append(message)
        )
        monkeypatch.setattr(cli, "_interactive", lambda: True)
        assert cli.main(["sync"]) == 2
        assert notified == []


@pytest.mark.parametrize(
    ("line", "trouble"),
    [
        ("Sync run complete: 0 synced, 9 skipped, 0 failed, 0 deferred", False),
        ("Routine sync done - created=0 updated=0 skipped=0 failed=0 scheduled=0", False),
        ("Authenticated successfully", False),
        ("Sync run complete: 0 synced, 8 skipped, 1 failed", True),
        ("Upload failed for workout abc: HTTP 500", True),
        ("Traceback (most recent call last):", True),
    ],
)
def test_hevy2garmin_lines_are_only_warnings_when_something_went_wrong(line, trouble):
    from gh_sync import h2g

    assert h2g.is_trouble(line) is trouble
