"""Telling someone when an unattended run fails.

A background sync that stops working is invisible: the most common cause, an
expired Garmin sign-in, can go unnoticed for weeks. So a failed scheduled run
raises a desktop notification, and optionally a push to ``GH_NOTIFY_URL`` (an
ntfy.sh topic URL, or anything that accepts a plain-text POST) for headless
machines and containers.

Notifications are rate limited through the ledger: one when a failure starts,
then at most one a day while it persists. Everything here is best effort; a
notification that cannot be delivered is logged and otherwise ignored.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from . import state

logger = logging.getLogger("gh_sync.notify")

TITLE = "garmin-hevy-sync"
REPEAT_AFTER = timedelta(hours=24)
_LAST_NOTICE = "failure_notified_at"
_LAST_MESSAGE = "failure_message"

_WINDOWS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:GHS_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:GHS_MESSAGE)) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def _run(command: list[str], env: dict[str, str] | None = None) -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=20, env=env, creationflags=flags
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Notification command failed: %s", exc)
        return False
    return result.returncode == 0


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def desktop(title: str, message: str) -> bool:
    if sys.platform == "darwin":
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        return _run(["osascript", "-e", script])
    if sys.platform == "win32":
        env = dict(os.environ, GHS_TITLE=title, GHS_MESSAGE=message)
        return _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TOAST], env=env
        )
    if shutil.which("notify-send"):
        env = dict(os.environ)
        # A systemd user service often lacks the session bus address even
        # though the desktop's bus is right there.
        bus = Path(f"/run/user/{os.getuid()}/bus")
        if "DBUS_SESSION_BUS_ADDRESS" not in env and bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        return _run(["notify-send", "--app-name", TITLE, title, message], env=env)
    return False


def push(url: str, title: str, message: str) -> bool:
    try:
        resp = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "warning", "Content-Type": "text/plain"},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning("Could not send notification to GH_NOTIFY_URL: %s", exc)
        return False
    if not resp.ok:
        logger.warning("GH_NOTIFY_URL answered %s", resp.status_code)
    return resp.ok


def should_notify(conn: sqlite3.Connection, message: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    last = state.get_meta(conn, _LAST_NOTICE)
    if last is None or state.get_meta(conn, _LAST_MESSAGE) != message:
        return True
    try:
        return now - datetime.fromisoformat(last) >= REPEAT_AFTER
    except ValueError:
        return True


def failure(conn: sqlite3.Connection, message: str, notify_url: str = "") -> None:
    """Report a failed unattended run, at most once a day per distinct problem."""
    if not should_notify(conn, message):
        logger.info("Failure already notified recently; not repeating")
        return
    text = f"Sync failed: {message}"
    delivered = desktop(TITLE, text)
    if notify_url:
        delivered = push(notify_url, TITLE, text) or delivered
    logger.info("Failure notification %s", "sent" if delivered else "could not be delivered")
    state.set_meta(conn, _LAST_NOTICE, datetime.now(UTC).isoformat())
    state.set_meta(conn, _LAST_MESSAGE, message)


def recovered(conn: sqlite3.Connection) -> None:
    """Forget an earlier failure so the next one notifies straight away."""
    if state.get_meta(conn, _LAST_NOTICE) is not None:
        state.set_meta(conn, _LAST_NOTICE, None)
        state.set_meta(conn, _LAST_MESSAGE, None)
