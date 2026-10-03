"""Running the sync in the background, on whatever the machine already has.

    Linux    systemd user timer (cron when there is no systemd user session)
    macOS    launchd agent in ~/Library/LaunchAgents
    Windows  Task Scheduler task, run windowless through pythonw.exe
    Docker   nothing to install: the container runs `sync --every` itself

Every backend runs ``<this python> -m gh_sync sync``. Storing the interpreter
rather than a console script keeps the entry valid across reinstalls of the
same tool environment and lets Windows use the windowless ``pythonw.exe``.

Content generation is kept in pure functions so each backend's output can be
tested on any operating system.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .config import ConfigError, paths, running_in_container

NAME = "garmin-hevy-sync"
MAC_LABEL = "io.github.danieltyukov.garmin-hevy-sync"
SITE_URL = "https://danieltyukov.github.io/garmin-hevy-sync/"
DEFAULT_MINUTES = 30
# Every run signs in to Garmin and pages through recent activities. More often
# than this buys nothing and invites Garmin's rate limiter.
MIN_MINUTES = 15
CRON_MARKER = f"# {NAME}"


@dataclass
class Result:
    backend: str
    installed: bool
    lines: list[str] = field(default_factory=list)


# --------------------------------------------------------------- the command


def sync_command(background: bool = True) -> list[str]:
    exe = Path(sys.executable)
    if sys.platform == "win32" and background:
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            exe = windowless
    # Always name the home folder. Schedulers start with a minimal environment
    # (no XDG_CONFIG_HOME, no GH_SYNC_HOME), so a default worked out there can
    # differ from the folder setup just wrote the key into.
    return [str(exe), "-m", "gh_sync", "--home", str(paths().home), "sync"]


def validate_minutes(minutes: int) -> int:
    if minutes < MIN_MINUTES:
        raise ConfigError(f"The interval must be at least {MIN_MINUTES} minutes.")
    divides_hour = minutes < 60 and 60 % minutes == 0
    divides_day = minutes % 60 == 0 and 24 % (minutes // 60) == 0
    if not (divides_hour or divides_day):
        raise ConfigError(
            "Pick an interval that divides an hour or a day evenly: "
            "15m, 20m, 30m, 1h, 2h, 3h, 4h, 6h, 8h, 12h or 24h."
        )
    return minutes


def detect_backend() -> str:
    if running_in_container():
        return "container"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "win32":
        return "schtasks"
    if _systemd_user_available():
        return "systemd"
    if shutil.which("crontab"):
        return "cron"
    return "none"


def _systemd_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        probe = subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    # errors="replace": schtasks prints in the console code page, which on a
    # localized Windows is not the locale encoding Python decodes with.
    return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60)


# -------------------------------------------------------------------- systemd


def _systemd_quote(arg: str) -> str:
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in " \t\"\\'$;"):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def on_calendar(minutes: int) -> str:
    if minutes < 60:
        return f"*:0/{minutes}"
    hours = minutes // 60
    return "*-*-* 00:00:00" if hours == 24 else f"*-*-* 0/{hours}:00:00"


def systemd_units(command: list[str], minutes: int) -> tuple[str, str]:
    service = f"""\
[Unit]
Description=Sync workouts between Garmin Connect and Hevy
Documentation={SITE_URL}
# Both APIs are remote, so there is no point starting before the network is up.
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={" ".join(_systemd_quote(part) for part in command)}
# A sync normally takes under a minute. This sits above the per-child limit
# in h2g.py so a hung hevy2garmin is stopped by us, and the run still gets
# recorded and reported, rather than by systemd.
TimeoutStartSec=60min
# Garmin rate-limits aggressively. The next timer tick is the retry.
Restart=no
Nice=10
"""
    timer = f"""\
[Unit]
Description=Run garmin-hevy-sync every {minutes} minutes
Documentation={SITE_URL}

[Timer]
OnCalendar={on_calendar(minutes)}
# Catch up on a run missed while the machine was asleep or powered off.
Persistent=true
# Spread the load so Garmin does not see every install at the same second.
RandomizedDelaySec={max(1, min(5, minutes // 6))}min
AccuracySec=1min

[Install]
WantedBy=timers.target
"""
    return service, timer


def _systemd_dir() -> Path:
    # Where the user manager looks by default. Deliberately not this shell's
    # XDG_CONFIG_HOME: the manager does not inherit it, so units written
    # there would never be found.
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_install(minutes: int, dry_run: bool) -> Result:
    service, timer = systemd_units(sync_command(), minutes)
    unit_dir = _systemd_dir()
    result = Result("systemd", installed=not dry_run)
    if dry_run:
        result.lines += [f"Would write {unit_dir / (NAME + '.service')}:", service]
        result.lines += [f"Would write {unit_dir / (NAME + '.timer')}:", timer]
        return result
    unit_dir.mkdir(parents=True, exist_ok=True)
    for suffix, content in ((".service", service), (".timer", timer)):
        target = unit_dir / f"{NAME}{suffix}"
        target.unlink(missing_ok=True)  # 0.1 may have left a symlink here
        target.write_text(content, encoding="utf-8")
        result.lines.append(f"Wrote {target}")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{NAME}.timer"],
    ):
        done = _run(command)
        if done.returncode != 0:
            raise RuntimeError(f"{' '.join(command)} failed: {done.stderr.strip()}")
    result.lines.append(f"Enabled {NAME}.timer (every {minutes} minutes)")
    result.lines += _ensure_linger()
    return result


def _ensure_linger() -> list[str]:
    """Without lingering, user timers stop when the last session ends."""
    user = getpass.getuser()
    probe = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    if probe.returncode == 0 and probe.stdout.strip() == "yes":
        return []
    attempt = _run(["loginctl", "enable-linger", user])
    if attempt.returncode == 0:
        return ["Enabled lingering, so the timer also runs while you are logged out."]
    return [
        "The timer runs while you are logged in. To keep it running while logged out:",
        f"  sudo loginctl enable-linger {user}",
    ]


def _systemd_remove() -> Result:
    result = Result("systemd", installed=False)
    _run(["systemctl", "--user", "disable", "--now", f"{NAME}.timer"])
    for suffix in (".service", ".timer"):
        target = _systemd_dir() / f"{NAME}{suffix}"
        if target.exists() or target.is_symlink():
            target.unlink()
            result.lines.append(f"Removed {target}")
    _run(["systemctl", "--user", "daemon-reload"])
    return result


def _systemd_status() -> Result:
    timer = f"{NAME}.timer"
    enabled = _run(["systemctl", "--user", "is-enabled", timer]).stdout.strip()
    result = Result("systemd", installed=enabled == "enabled")
    if result.installed:
        show = _run(
            [
                "systemctl",
                "--user",
                "show",
                timer,
                "-p",
                "NextElapseUSecRealtime",
                "-p",
                "LastTriggerUSec",
            ]
        ).stdout
        for line in show.splitlines():
            key, _, value = line.partition("=")
            label = {"NextElapseUSecRealtime": "next run", "LastTriggerUSec": "last run"}.get(key)
            if label and value:
                result.lines.append(f"{label}: {value}")
    return result


# ----------------------------------------------------------------------- cron


def cron_line(command: list[str], minutes: int) -> str:
    if minutes < 60:
        when = f"*/{minutes} * * * *"
    else:
        hours = minutes // 60
        when = "0 0 * * *" if hours == 24 else f"0 */{hours} * * *"
    return f"{when} {shlex.join(command)} >/dev/null 2>&1 {CRON_MARKER}"


def _crontab_lines() -> list[str]:
    done = _run(["crontab", "-l"])
    if done.returncode != 0:
        return []  # "no crontab for user"
    return done.stdout.splitlines()


def _write_crontab(lines: list[str]) -> None:
    done = subprocess.run(
        ["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True
    )
    if done.returncode != 0:
        raise RuntimeError(f"crontab failed: {done.stderr.strip()}")


def _cron_install(minutes: int, dry_run: bool) -> Result:
    line = cron_line(sync_command(), minutes)
    result = Result("cron", installed=not dry_run)
    if dry_run:
        result.lines += ["Would add this crontab line:", line]
        return result
    lines = [entry for entry in _crontab_lines() if not entry.endswith(CRON_MARKER)]
    _write_crontab([*lines, line])
    result.lines += [
        f"Added a crontab entry (every {minutes} minutes).",
        "cron does not catch up on runs missed while the machine was off.",
    ]
    return result


def _cron_remove() -> Result:
    lines = _crontab_lines()
    kept = [entry for entry in lines if not entry.endswith(CRON_MARKER)]
    result = Result("cron", installed=False)
    if len(kept) != len(lines):
        _write_crontab(kept)
        result.lines.append("Removed the crontab entry.")
    return result


def _cron_status() -> Result:
    entries = [entry for entry in _crontab_lines() if entry.endswith(CRON_MARKER)]
    return Result("cron", installed=bool(entries), lines=entries)


# -------------------------------------------------------------------- launchd


def launchd_plist(command: list[str], minutes: int, log_dir: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": MAC_LABEL,
            "ProgramArguments": command,
            "StartInterval": minutes * 60,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            # The tool keeps its own rotating log; stderr only catches crashes
            # that happen before logging is set up.
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(log_dir / "launchd-errors.log"),
        }
    )


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_install(minutes: int, dry_run: bool) -> Result:
    content = launchd_plist(sync_command(), minutes, paths().log_dir)
    target = _plist_path()
    result = Result("launchd", installed=not dry_run)
    if dry_run:
        result.lines += [f"Would write {target}:", content.decode()]
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    paths().log_dir.mkdir(parents=True, exist_ok=True)
    _run(["launchctl", "bootout", f"{_gui_domain()}/{MAC_LABEL}"])
    target.write_bytes(content)
    result.lines.append(f"Wrote {target}")
    loaded = _run(["launchctl", "bootstrap", _gui_domain(), str(target)])
    if loaded.returncode != 0:
        # Older macOS, or no GUI session (e.g. over SSH): the legacy verb still works.
        legacy = _run(["launchctl", "load", "-w", str(target)])
        if legacy.returncode != 0:
            raise RuntimeError(
                f"launchctl could not load the agent: {loaded.stderr.strip() or legacy.stderr}"
            )
    result.lines.append(f"Loaded {MAC_LABEL} (every {minutes} minutes)")
    return result


def _launchd_remove() -> Result:
    result = Result("launchd", installed=False)
    target = _plist_path()
    if _run(["launchctl", "bootout", f"{_gui_domain()}/{MAC_LABEL}"]).returncode != 0:
        _run(["launchctl", "unload", "-w", str(target)])
    if target.exists():
        target.unlink()
        result.lines.append(f"Removed {target}")
    return result


def _launchd_status() -> Result:
    target = _plist_path()
    printed = _run(["launchctl", "print", f"{_gui_domain()}/{MAC_LABEL}"])
    result = Result("launchd", installed=target.exists() and printed.returncode == 0)
    for line in printed.stdout.splitlines():
        line = line.strip()
        if line.startswith(("last exit code", "runs =", "run interval")):
            result.lines.append(line)
    if target.exists() and printed.returncode != 0:
        result.lines.append(f"{target} exists but is not loaded")
    return result


# ------------------------------------------------------------ Task Scheduler


def task_xml(command: list[str], minutes: int, start: datetime | None = None) -> str:
    start = (start or datetime.now()).replace(second=0, microsecond=0)
    program, arguments = command[0], subprocess.list2cmdline(command[1:])
    return f"""\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Sync workouts between Garmin Connect and Hevy every {minutes} minutes. Installed by garmin-hevy-sync; remove with "garmin-hevy-sync schedule remove".</Description>
    <URI>\\{NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <RandomDelay>PT{max(1, min(5, minutes // 6))}M</RandomDelay>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks_install(minutes: int, dry_run: bool) -> Result:
    xml = task_xml(sync_command(), minutes)
    result = Result("schtasks", installed=not dry_run)
    if dry_run:
        result.lines += [f"Would register scheduled task {NAME!r}:", xml]
        return result
    with tempfile.TemporaryDirectory() as tmp:
        definition = Path(tmp) / "task.xml"
        definition.write_bytes(xml.encode("utf-16"))
        done = _run(["schtasks", "/Create", "/TN", NAME, "/XML", str(definition), "/F"])
    if done.returncode != 0:
        raise RuntimeError(f"schtasks failed: {(done.stderr or done.stdout).strip()}")
    result.lines.append(f"Registered scheduled task {NAME!r} (every {minutes} minutes)")
    return result


def _schtasks_remove() -> Result:
    result = Result("schtasks", installed=False)
    if _run(["schtasks", "/Delete", "/TN", NAME, "/F"]).returncode == 0:
        result.lines.append(f"Removed scheduled task {NAME!r}")
    return result


def _schtasks_status() -> Result:
    done = _run(["schtasks", "/Query", "/TN", NAME, "/V", "/FO", "LIST"])
    result = Result("schtasks", installed=done.returncode == 0)
    for line in done.stdout.splitlines():
        if line.strip().startswith(("Next Run Time", "Last Run Time", "Last Result", "Status")):
            result.lines.append(" ".join(line.split()))
    return result


# ------------------------------------------------------------------ dispatch


def _container(_: object = None, __: object = None) -> Result:
    return Result(
        "container",
        installed=True,
        lines=["In Docker the container schedules itself (`sync --every 30m`)."],
    )


def _none(*_: object) -> Result:
    raise RuntimeError(
        "No scheduler found (no systemd user session and no crontab). Run "
        "`garmin-hevy-sync sync --every 30m` under any process supervisor instead."
    )


_INSTALL = {
    "systemd": _systemd_install,
    "cron": _cron_install,
    "launchd": _launchd_install,
    "schtasks": _schtasks_install,
    "container": _container,
    "none": _none,
}
_REMOVE = {
    "systemd": _systemd_remove,
    "cron": _cron_remove,
    "launchd": _launchd_remove,
    "schtasks": _schtasks_remove,
    "container": _container,
    "none": lambda: Result("none", installed=False),
}
_STATUS = {
    "systemd": _systemd_status,
    "cron": _cron_status,
    "launchd": _launchd_status,
    "schtasks": _schtasks_status,
    "container": _container,
    "none": lambda: Result("none", installed=False, lines=["no scheduler available"]),
}


def install(minutes: int = DEFAULT_MINUTES, dry_run: bool = False) -> Result:
    validate_minutes(minutes)
    return _INSTALL[detect_backend()](minutes, dry_run)


def remove() -> Result:
    return _REMOVE[detect_backend()]()


def status() -> Result:
    return _STATUS[detect_backend()]()
