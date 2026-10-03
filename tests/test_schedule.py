from __future__ import annotations

import plistlib
import shlex
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from gh_sync import schedule
from gh_sync.config import ConfigError

COMMAND = ["/opt/tools/My Python/bin/python", "-m", "gh_sync", "sync"]


class TestValidateMinutes:
    @pytest.mark.parametrize("minutes", [15, 20, 30, 60, 120, 180, 360, 720, 1440])
    def test_even_divisors_pass(self, minutes):
        assert schedule.validate_minutes(minutes) == minutes

    @pytest.mark.parametrize("minutes", [5, 14, 25, 45, 90, 300])
    def test_others_are_refused(self, minutes):
        with pytest.raises(ConfigError):
            schedule.validate_minutes(minutes)


class TestSyncCommand:
    def test_runs_the_module_through_this_interpreter(self, isolated_home):
        command = schedule.sync_command()
        assert command[1:] == ["-m", "gh_sync", "--home", str(isolated_home), "sync"]

    def test_always_names_the_home_folder(self, monkeypatch, tmp_path):
        """Schedulers lack this shell's XDG_CONFIG_HOME, so the default is not enough."""
        monkeypatch.delenv("GH_SYNC_HOME")
        monkeypatch.setattr(schedule.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-xdg"))
        command = schedule.sync_command()
        assert command[-3:] == ["--home", str(tmp_path / "custom-xdg" / "garmin-hevy-sync"), "sync"]


class TestSystemd:
    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [(30, "*:0/30"), (15, "*:0/15"), (60, "*-*-* 0/1:00:00"), (1440, "*-*-* 00:00:00")],
    )
    def test_on_calendar(self, minutes, expected):
        assert schedule.on_calendar(minutes) == expected

    def test_units(self):
        service, timer = schedule.systemd_units(COMMAND, 30)
        assert 'ExecStart="/opt/tools/My Python/bin/python" -m gh_sync sync' in service
        assert "Type=oneshot" in service
        assert "OnCalendar=*:0/30" in timer
        # Persistent= only works with OnCalendar=, which is why the timer uses it.
        assert "Persistent=true" in timer
        assert "WantedBy=timers.target" in timer

    def test_percent_signs_are_escaped(self):
        service, _ = schedule.systemd_units(["/x/100%/python", "-m", "gh_sync", "sync"], 30)
        assert "/x/100%%/python" in service


class TestCron:
    def test_minutes(self):
        line = schedule.cron_line(COMMAND, 30)
        assert line.startswith("*/30 * * * * ")
        assert line.endswith(schedule.CRON_MARKER)
        assert shlex.quote(COMMAND[0]) in line

    def test_hours_and_daily(self):
        assert schedule.cron_line(COMMAND, 120).startswith("0 */2 * * * ")
        assert schedule.cron_line(COMMAND, 1440).startswith("0 0 * * * ")


class TestLaunchd:
    def test_plist(self, tmp_path):
        data = plistlib.loads(schedule.launchd_plist(COMMAND, 30, tmp_path))
        assert data["Label"] == schedule.MAC_LABEL
        assert data["ProgramArguments"] == COMMAND
        assert data["StartInterval"] == 1800
        assert data["RunAtLoad"] is True
        assert data["StandardErrorPath"] == str(tmp_path / "launchd-errors.log")


class TestTaskScheduler:
    NS: ClassVar[dict[str, str]] = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def _parse(self, command, minutes=30):
        xml = schedule.task_xml(command, minutes, start=datetime(2026, 10, 3, 12, 0))
        # The declaration says UTF-16 because that is how it is written to disk.
        return ET.fromstring(xml.encode("utf-16"))

    def test_trigger_and_settings(self):
        root = self._parse(COMMAND)
        assert root.find(".//t:Repetition/t:Interval", self.NS).text == "PT30M"
        assert root.find(".//t:StartBoundary", self.NS).text == "2026-10-03T12:00:00"
        assert root.find(".//t:StartWhenAvailable", self.NS).text == "true"
        assert root.find(".//t:MultipleInstancesPolicy", self.NS).text == "IgnoreNew"
        assert root.find(".//t:DisallowStartIfOnBatteries", self.NS).text == "false"
        assert root.find(".//t:LogonType", self.NS).text == "InteractiveToken"

    def test_action(self):
        command = [r"C:\Users\A B\pythonw.exe", "-m", "gh_sync", "--home", r"C:\x y", "sync"]
        root = self._parse(command)
        assert root.find(".//t:Exec/t:Command", self.NS).text == command[0]
        assert (
            root.find(".//t:Exec/t:Arguments", self.NS).text == '-m gh_sync --home "C:\\x y" sync'
        )

    def test_special_characters_are_escaped(self):
        root = self._parse([r"C:\R&D\pythonw.exe", "-m", "gh_sync", "sync"])
        assert root.find(".//t:Exec/t:Command", self.NS).text == r"C:\R&D\pythonw.exe"


class TestBackendSelection:
    def test_container(self, monkeypatch):
        monkeypatch.setenv("GH_SYNC_CONTAINER", "1")
        assert schedule.detect_backend() == "container"
        result = schedule.install(30)
        assert result.backend == "container"

    def test_dry_run_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(schedule, "detect_backend", lambda: "systemd")
        result = schedule.install(30, dry_run=True)
        assert not result.installed
        assert any("OnCalendar" in line for line in result.lines)
        assert not (Path.home() / ".config" / "systemd").exists()

    def test_install_validates_before_touching_anything(self, monkeypatch):
        monkeypatch.setattr(schedule, "detect_backend", lambda: "systemd")
        with pytest.raises(ConfigError):
            schedule.install(45, dry_run=True)

    def test_systemd_install_writes_units(self, monkeypatch, tmp_path):
        monkeypatch.setattr(schedule, "detect_backend", lambda: "systemd")
        # The user manager does not see this shell's XDG_CONFIG_HOME, so the
        # units must go to ~/.config regardless.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
        calls = []

        class Done:
            returncode = 0
            stdout = "yes"
            stderr = ""

        monkeypatch.setattr(schedule, "_run", lambda command: calls.append(command) or Done())
        result = schedule.install(30)
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        assert (unit_dir / "garmin-hevy-sync.timer").read_text().count("OnCalendar") == 1
        assert ["systemctl", "--user", "enable", "--now", "garmin-hevy-sync.timer"] in calls
        assert result.installed
