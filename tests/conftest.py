from __future__ import annotations

import subprocess

import pytest

from gh_sync import config, h2g, notify, schedule


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every path the tool uses at a throwaway directory.

    The home folder, the Garmin token store and hevy2garmin's ~/.hevy2garmin
    all resolve under tmp_path, so no test can read or write a real install.
    Side effects that would leave the machine are stubbed as a safety net:
    the 0.1 migration source (a developer's own checkout may hold a real .env),
    the system scheduler, desktop and push notifications, and the hevy2garmin
    child process. Tests that exercise one of these patch it again themselves.
    """
    monkeypatch.setattr(config, "_CHECKOUT_ROOT", tmp_path / "no-checkout")
    monkeypatch.setattr(schedule, "_systemd_user_available", lambda: False)
    monkeypatch.setattr(
        schedule,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 1, stdout="", stderr="stubbed"),
    )
    monkeypatch.setattr(notify, "desktop", lambda title, message: False)
    monkeypatch.setattr(notify, "push", lambda url, title, message: False)
    monkeypatch.setattr(h2g, "hevy2garmin_command", lambda: None)

    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_home / ".config"))
    monkeypatch.setenv("GH_SYNC_HOME", str(tmp_path / "gh-home"))
    monkeypatch.setenv("GARMINTOKENS", str(user_home / ".garminconnect"))
    for name in (
        "HEVY_API_KEY",
        "GARMIN_EMAIL",
        "GARMIN_PASSWORD",
        "GH_LOOKBACK_DAYS",
        "GH_BODY_LOOKBACK_DAYS",
        "GH_OVERLAP_MINUTES",
        "GH_IMPORT_DELAY_MINUTES",
        "GH_MATCH_THRESHOLD",
        "GH_IMPORT_PRIVATE",
        "GH_NOTIFY_URL",
        "GH_SYNC_CONTAINER",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "gh-home"
