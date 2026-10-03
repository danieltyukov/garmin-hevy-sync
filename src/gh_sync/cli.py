"""Command line entry point and the orchestrator for the five sync flows.

Flow order is load-bearing. A runs before B so that Hevy workouts reach Garmin
first, merging into whatever the watch recorded. By the time B looks at Garmin
activities, anything that came from Hevy already has a Hevy counterpart within
the overlap window and is skipped. Running B first would import a watch session
into Hevy that A was about to enrich, producing a duplicate.

E runs last, because it repairs what A wrote and wants A's PUT to have landed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import __version__, h2g, notify, schedule, state
from .config import (
    HOME_ENV,
    ConfigError,
    Settings,
    ensure_home,
    garmin_token_dir,
    hevy2garmin_home,
    migrate_legacy_layout,
    parse_interval,
    paths,
    update_env_file,
)
from .flows import flow_b_garmin_to_hevy, flow_d_body_measurements, flow_e_exercise_names
from .garmin_client import (
    GarminLoginRequired,
    GarminUnavailable,
    has_token_store,
    resume,
    sign_in,
    strength_activities,
)
from .hevy import HevyClient
from .lock import AlreadyRunning, run_lock

logger = logging.getLogger("gh_sync")

FLOWS = ("a", "b", "c", "d", "e")
FLOW_LABELS = {
    "a_hevy_to_garmin": "A  Hevy workouts -> Garmin",
    "c_routines_to_garmin": "C  Hevy routines -> Garmin",
    "b_garmin_to_hevy": "B  watch sessions -> Hevy",
    "d_body_measurements": "D  weigh-ins -> Hevy",
    "e_exercise_names": "E  exercise names repaired",
}


def _interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


class _RotatingLog(RotatingFileHandler):
    """Rotation that survives the file being held open elsewhere.

    On Windows a rename fails while another process has the file open, for
    example `garmin-hevy-sync logs -f` in another window. The stock handler
    then drops every record until the rename succeeds; this one keeps writing
    to the current file and tries to rotate again on the next record.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def setup_logging(verbose: bool, console_level: int = logging.INFO) -> None:
    p = ensure_home()
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    # Rotate rather than append forever: a run every 30 minutes writes ~30
    # lines, so five 5 MB generations is roughly a year of history.
    file_handler = _RotatingLog(
        p.log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(fmt))
    handlers: list[logging.Handler] = [file_handler]
    # pythonw.exe (the windowless scheduled run on Windows) has no stdout.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else console_level)
        console.setFormatter(logging.Formatter(fmt))
        handlers.append(console)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    # These are chatty at DEBUG and drown out our own lines.
    for noisy in ("urllib3", "requests", "garminconnect", "garmin_auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------- sync


def _pick_problem(problems: list[str]) -> str:
    """The most actionable problem goes in the notification."""
    for problem in problems:
        if "login" in problem.lower() or "sign-in" in problem.lower():
            return problem
    return problems[0]


def _run_flows(args: argparse.Namespace, background: bool) -> int:
    settings = Settings.load(dry_run=args.dry_run)
    selected = set(args.flows or FLOWS)
    summary: dict[str, Any] = {}
    problems: list[str] = []

    with state.connect() as conn:
        run_id = state.start_run(conn)

        for flow, key, command in (
            ("a", "a_hevy_to_garmin", ["sync"]),
            ("c", "c_routines_to_garmin", ["sync-routines"]),
        ):
            if flow in selected:
                ok = h2g.run_logged(command, dry_run=args.dry_run)
                summary[key] = "ok" if ok else "failed"
                if not ok:
                    problems.append(f"flow {flow.upper()} (hevy2garmin {command[0]}) failed")

        if selected & {"b", "d", "e"}:
            try:
                hevy = HevyClient(settings.hevy_api_key)
                garmin = resume()
            except (GarminLoginRequired, GarminUnavailable) as exc:
                logger.error("%s", exc)
                problems.append(str(exc))
                for flow, key in (
                    ("b", "b_garmin_to_hevy"),
                    ("d", "d_body_measurements"),
                    ("e", "e_exercise_names"),
                ):
                    if flow in selected:
                        summary[key] = "not run: Garmin unavailable"
            else:
                # E runs last on purpose. It reads back what A wrote, and Garmin
                # needs a moment before a PUT is visible to a GET; letting B and
                # D do their API work first buys that settling time without a
                # bare sleep. If it still reads too early it records nothing and
                # the next run picks the activity up.
                steps: list[tuple[str, str, Callable[[], dict[str, int]]]] = [
                    (
                        "b",
                        "b_garmin_to_hevy",
                        lambda: flow_b_garmin_to_hevy(garmin, hevy, conn, settings),
                    ),
                    (
                        "d",
                        "d_body_measurements",
                        lambda: flow_d_body_measurements(garmin, hevy, conn, settings),
                    ),
                    (
                        "e",
                        "e_exercise_names",
                        lambda: flow_e_exercise_names(garmin, conn, settings),
                    ),
                ]
                for flow, key, step in steps:
                    if flow not in selected:
                        continue
                    # One flow failing must not take the others down with it.
                    try:
                        summary[key] = step()
                    except Exception as exc:
                        logger.exception("Flow %s failed", flow.upper())
                        summary[key] = f"failed: {exc}"
                        problems.append(f"flow {flow.upper()}: {exc}")

        ok = not problems
        state.finish_run(conn, run_id, json.dumps(summary, default=str), ok)
        state.prune_runs(conn)
        if background and not args.dry_run:
            if problems:
                notify.failure(conn, _pick_problem(problems), settings.notify_url)
            else:
                notify.recovered(conn)

    logger.info("Sync summary: %s", json.dumps(summary, default=str))
    return 0 if ok else 1


def _record_failure(exc: BaseException) -> None:
    """Leave a trace of a background run that failed before the flows could.

    A missing key or a crash under cron or pythonw has nowhere to print to,
    so it becomes a failed run in the ledger (visible in `status`) and a
    notification, the same as a flow failure would.
    """
    message = str(exc) if isinstance(exc, ConfigError) else f"{type(exc).__name__}: {exc}"
    try:
        with state.connect() as conn:
            run_id = state.start_run(conn)
            state.finish_run(conn, run_id, json.dumps({"error": message}), ok=False)
            notify.failure(conn, message, os.environ.get("GH_NOTIFY_URL", "").strip())
    except Exception:
        logger.exception("Could not record the failed run")


def _sync_once(args: argparse.Namespace, background: bool) -> int:
    started = time.monotonic()
    try:
        with run_lock(paths().lock_file):
            code = _run_flows(args, background)
    except AlreadyRunning as exc:
        if background:
            logger.info("%s Skipping this run.", exc)
            return 0
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        if background and not args.dry_run:
            _record_failure(exc)
        raise
    logger.info("Finished in %.1fs", time.monotonic() - started)
    return code


def _loop(args: argparse.Namespace, minutes: int) -> int:
    """Sync forever. What the Docker image runs, and handy under any supervisor."""
    # As PID 1 in a container, SIGTERM is ignored unless something handles it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    logger.info("Syncing every %s minutes. Stop with Ctrl+C.", minutes)
    while True:
        started = time.monotonic()
        try:
            _sync_once(args, background=True)
        except ConfigError as exc:
            # Keep looping: the config file may be fixed while we wait, and a
            # crash-looping container helps nobody.
            logger.error("%s", exc)
        except Exception:
            logger.exception("Sync run crashed")
        time.sleep(max(60.0, minutes * 60 - (time.monotonic() - started)))


def cmd_sync(args: argparse.Namespace) -> int:
    if args.every:
        minutes = parse_interval(args.every)
        if minutes < schedule.MIN_MINUTES:
            raise ConfigError(f"--every must be at least {schedule.MIN_MINUTES} minutes.")
        return _loop(args, minutes)
    return _sync_once(args, background=not _interactive())


# -------------------------------------------------------------------- status


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    local = moment.astimezone().strftime("%Y-%m-%d %H:%M")
    if seconds < 90:
        return f"{local} (just now)"
    if seconds < 5400:
        return f"{local} ({seconds // 60} min ago)"
    if seconds < 172800:
        return f"{local} ({seconds // 3600} h ago)"
    return f"{local} ({seconds // 86400} days ago)"


def _describe(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items())
    return str(value)


def _print_schedule() -> None:
    try:
        result = schedule.status()
    except Exception as exc:
        print(f"Schedule   could not check ({exc})")
        return
    state_text = "on" if result.installed else "off"
    print(f"Schedule   {state_text} ({result.backend})")
    for line in result.lines:
        print(f"           {line}")
    if not result.installed and result.backend not in ("container", "none"):
        print("           turn it on with `garmin-hevy-sync schedule install`")


def cmd_status(_: argparse.Namespace) -> int:
    p = paths()
    print(f"garmin-hevy-sync {__version__}")
    print(f"Home       {p.home}")
    _print_schedule()
    if not p.state_db.exists():
        print("\nNo sync has run yet. Start one with `garmin-hevy-sync sync`.")
        return 0
    with state.connect() as conn:
        last = state.last_run(conn)
        if last:
            outcome = {1: "ok", 0: "failed"}.get(
                last["ok"], "incomplete" if not last["ended_at"] else ""
            )
            print(f"Last run   {_ago(last['started_at'])}{', ' + outcome if outcome else ''}")
            try:
                summary = json.loads(last["summary"] or "{}")
            except ValueError:
                summary = {}
            for key, value in summary.items():
                print(f"           {FLOW_LABELS.get(key, key)}: {_describe(value)}")

        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM garmin_to_hevy GROUP BY status"
        ).fetchall()
        counts = ", ".join(f"{row['n']} {row['status']}" for row in rows) or "none yet"
        measurements = conn.execute("SELECT COUNT(*) AS n FROM body_measurements").fetchone()
        named = conn.execute("SELECT COUNT(*) AS n FROM exercise_names_fixed").fetchone()
        print("\nAll time")
        print(f"  Watch sessions (flow B)   {counts}")
        print(f"  Weigh-ins (flow D)        {measurements['n']} synced")
        print(f"  Name repairs (flow E)     {named['n']} activities checked or fixed")

        recent = conn.execute(
            "SELECT garmin_activity_id, status, hevy_workout_id, note "
            "FROM garmin_to_hevy ORDER BY synced_at DESC LIMIT 5"
        ).fetchall()
        if recent:
            print("\nRecent watch sessions")
            for row in recent:
                detail = row["hevy_workout_id"] or row["note"] or ""
                print(f"  {row['garmin_activity_id']:<14} {row['status']:<9} {detail}")
    return 0


def cmd_unmapped(_: argparse.Namespace) -> int:
    map_file = paths().exercise_map
    if not map_file.exists():
        print(f"No exercise map yet at {map_file}. It is created by the first sync.")
        return 0
    data = json.loads(map_file.read_text(encoding="utf-8"))
    unmapped = data.get("unmapped", {})
    resolved = data.get("resolved", {})
    print(f"{len(resolved)} Garmin exercises mapped, {len(unmapped)} unmapped.\n")
    if not unmapped:
        print("Nothing needs attention.")
        return 0
    print("Unmapped (add these to 'overrides' in the map file to fix):")
    for key, info in sorted(unmapped.items(), key=lambda kv: -kv[1].get("seen", 0)):
        print(
            f"  {key:<40} seen={info.get('seen', 0):<3} "
            f"closest={info.get('closest', '?')} ({info.get('score', 0)})"
        )
    print(f"\nEdit {map_file}")
    return 0


# -------------------------------------------------------------------- doctor


def cmd_doctor(_: argparse.Namespace) -> int:
    """Check the installation, both credentials and the schedule."""
    ok = True
    p = paths()
    print("Environment")
    print(
        f"  garmin-hevy-sync {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.release()}"
    )
    print(f"  home: {p.home}")
    h2g_version = h2g.version()
    if h2g_version:
        print(f"  hevy2garmin {h2g_version}")
    else:
        print("  hevy2garmin: MISSING (flows A and C cannot run)")
        ok = False
    stored = hevy2garmin_home() / "config.json"
    try:
        if stored.exists() and json.loads(stored.read_text(encoding="utf-8")).get(
            "garmin_password"
        ):
            print(f"  WARNING: {stored} holds your Garmin password in plain text.")
            print("           Run `garmin-hevy-sync setup` to remove it.")
    except (OSError, ValueError):
        pass

    try:
        settings = Settings.load(require_hevy=False)
    except ConfigError as exc:
        print(f"  config: {exc}")
        return 1

    print("\nHevy")
    if not settings.hevy_api_key:
        print("  no API key configured. Run `garmin-hevy-sync setup`.")
        ok = False
    else:
        try:
            hevy = HevyClient(settings.hevy_api_key)
            info = hevy.user_info()
            user = info.get("data", info)
            print(f"  connected as {user.get('name') or user.get('username') or '?'}")
            count = hevy.workout_count()
            templates = sum(1 for _ in hevy.iter_exercise_templates())
            routines = sum(1 for _ in hevy.iter_routines())
            print(f"  {count} workouts, {templates} exercise templates, {routines} routines")
        except Exception as exc:
            print(f"  FAILED: {exc}")
            print("  A 401 usually means a wrong key or a lapsed Hevy Pro subscription.")
            ok = False

    print("\nGarmin")
    print(f"  token store: {garmin_token_dir()}{'' if has_token_store() else ' (missing)'}")
    try:
        garmin = resume()
        print(f"  connected as {garmin.get_full_name()}")
        for device in garmin.get_devices() or []:
            print(f"  device: {device.get('displayName') or device.get('productDisplayName')}")
        activities = strength_activities(garmin, settings.lookback_days)
        print(f"  {len(activities)} strength activities in the last {settings.lookback_days} days")
        for activity in activities[-5:]:
            print(
                f"    {activity.get('startTimeGMT')}  {activity.get('activityName')} "
                f"(id {activity.get('activityId')})"
            )
    except Exception as exc:
        print(f"  FAILED: {exc}")
        ok = False

    print()
    _print_schedule()
    print("\nAll checks passed." if ok else "\nSome checks failed; see above.")
    return 0 if ok else 1


# --------------------------------------------------------------------- login


def _mfa_from_file(code_file: Path, timeout: int = 600) -> Callable[[], str]:
    """MFA code delivered through a file instead of a terminal.

    For an operator or an agent fetching the emailed code: start
    ``login --mfa-file PATH``, wait for MFA_REQUESTED, write the code to PATH.
    """

    def wait_for_code() -> str:
        print(f"MFA_REQUESTED writing_to={code_file}", flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if code_file.exists():
                code = code_file.read_text(encoding="utf-8").strip()
                if code:
                    code_file.unlink(missing_ok=True)
                    print(f"MFA_CODE_RECEIVED len={len(code)}", flush=True)
                    return code
            time.sleep(2)
        raise RuntimeError(f"No MFA code appeared in {code_file} within {timeout}s")

    return wait_for_code


def cmd_login(args: argparse.Namespace) -> int:
    """Interactive Garmin sign-in, including the emailed MFA code.

    Everything afterwards runs off the cached tokens, which refresh themselves.
    """
    settings = Settings.load(require_hevy=False)
    p = ensure_home()
    email = args.email or settings.garmin_email or input("Garmin Connect email: ").strip()
    password = settings.garmin_password or getpass.getpass("Garmin password (input hidden): ")
    prompt = None
    if args.mfa_file:
        code_file = Path(args.mfa_file)
        code_file.unlink(missing_ok=True)
        prompt = _mfa_from_file(code_file)
    print(f"Signing in to Garmin Connect as {email}")
    try:
        client = sign_in(email, password, prompt)
    except Exception as exc:
        print(f"\nLogin failed: {exc}")
        if "429" in str(exc) or "too many" in str(exc).lower():
            print("Garmin rate-limits sign-ins. Wait 15 minutes before trying again.")
        return 1
    update_env_file(p.config_env, {"GARMIN_EMAIL": email})
    print(f"\nSigned in as {client.get_full_name()}")
    print(f"Tokens cached in {garmin_token_dir()}; scheduled runs need no password.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_wizard import run

    return run(no_schedule=args.no_schedule, minutes=parse_interval(args.every))


# ---------------------------------------------------------------- push, logs


def cmd_push_to_watch(args: argparse.Namespace) -> int:
    """Push Garmin planned workouts to the watch immediately.

    Garmin normally delivers workouts on its own schedule; this forces them
    across now. Deliberately not part of the periodic sync, which would re-push
    the same workouts every 30 minutes.
    """
    garmin = resume()
    devices = garmin.get_devices() or []
    if not devices:
        print("No Garmin devices found on the account.")
        return 1
    device = devices[min(args.device, len(devices) - 1)]
    device_id = device.get("deviceId")
    print(f"Pushing to {device.get('displayName') or device_id}")

    workouts = garmin.get_workouts(0, args.limit) or []
    pushed = 0
    for workout in workouts:
        try:
            garmin.push_workout_to_device(workout.get("workoutId"), device_id)
            print(f"  pushed {workout.get('workoutName')}")
            pushed += 1
        except Exception as exc:
            print(f"  failed {workout.get('workoutName')}: {exc}")
    print(f"{pushed}/{len(workouts)} workouts pushed.")
    return 0 if pushed == len(workouts) else 1


def cmd_logs(args: argparse.Namespace) -> int:
    log_file = paths().log_file
    if not log_file.exists():
        print(f"No log yet at {log_file}.")
        return 0
    with open(log_file, encoding="utf-8", errors="replace") as handle:
        for line in deque(handle, maxlen=args.lines):
            print(line, end="")
        if not args.follow:
            return 0
        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
            return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    if args.action == "install":
        result = schedule.install(parse_interval(args.every), dry_run=args.dry_run)
    elif args.action == "remove":
        result = schedule.remove()
    else:
        result = schedule.status()
        print(f"{'on' if result.installed else 'off'} ({result.backend})")
    for line in result.lines:
        print(line)
    if args.action == "remove" and not result.lines:
        print("No schedule was installed.")
    return 0


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-hevy-sync",
        description="Two-way sync between a Garmin watch and Hevy.",
        epilog="Start with `garmin-hevy-sync setup`. Docs: "
        "https://danieltyukov.github.io/garmin-hevy-sync/",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--home", metavar="PATH", help=f"settings and state folder (env {HOME_ENV})"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("setup", help="interactive first-time setup (safe to re-run)")
    p.add_argument("--no-schedule", action="store_true", help="do not install the background sync")
    p.add_argument("--every", default="30m", help="background interval (default 30m)")
    p.set_defaults(func=cmd_setup, console=logging.WARNING)

    p = sub.add_parser("sync", help="run the sync flows (default: all five)")
    p.add_argument(
        "--flows",
        nargs="+",
        choices=FLOWS,
        metavar="FLOW",
        help="a=Hevy workouts->Garmin  b=watch sessions->Hevy  c=routines->Garmin  "
        "d=weigh-ins->Hevy  e=repair exercise names",
    )
    p.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    p.add_argument(
        "--every", metavar="INTERVAL", help="keep running, syncing every INTERVAL (e.g. 30m)"
    )
    p.set_defaults(func=cmd_sync, console=logging.INFO)

    p = sub.add_parser("status", help="last run, schedule and ledger totals")
    p.set_defaults(func=cmd_status, console=logging.WARNING)

    p = sub.add_parser("doctor", help="check the install, credentials and schedule")
    p.set_defaults(func=cmd_doctor, console=logging.WARNING)

    p = sub.add_parser("login", help="sign in to Garmin (handles the emailed MFA code)")
    p.add_argument("--email", help="Garmin Connect email (default: the configured one)")
    p.add_argument(
        "--mfa-file", metavar="PATH", help="read the MFA code from PATH instead of the terminal"
    )
    p.set_defaults(func=cmd_login, console=logging.WARNING)

    p = sub.add_parser("logs", help="show the sync log")
    p.add_argument("-n", "--lines", type=int, default=50, help="lines to show (default 50)")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    p.set_defaults(func=cmd_logs, console=logging.WARNING)

    p = sub.add_parser("unmapped", help="Garmin exercises with no confident Hevy match")
    p.set_defaults(func=cmd_unmapped, console=logging.WARNING)

    p = sub.add_parser("push-to-watch", help="send planned workouts to the watch now")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--device", type=int, default=0, help="device index if you have several")
    p.set_defaults(func=cmd_push_to_watch, console=logging.WARNING)

    p = sub.add_parser("schedule", help="background sync: install, remove or status")
    p.add_argument("action", choices=("install", "remove", "status"))
    p.add_argument("--every", default="30m", help="interval for install (default 30m)")
    p.add_argument("--dry-run", action="store_true", help="show what install would write")
    p.set_defaults(func=cmd_schedule, console=logging.WARNING)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.home:
        os.environ[HOME_ENV] = str(Path(args.home).expanduser().resolve())
    migrated = migrate_legacy_layout(paths())
    setup_logging(args.verbose, args.console)
    for note in migrated:
        logger.warning("Moved to the new layout: %s", note)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        _report(str(exc))
        return 2
    except (GarminLoginRequired, GarminUnavailable) as exc:
        _report(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Unexpected: keep the traceback, in the log file above all, because a
        # scheduled run's console goes nowhere.
        logger.exception("Unexpected error in `%s`", args.command)
        return 1


def _report(message: str) -> None:
    """An expected error: a clean line on stderr, and a record in the log file."""
    print(f"error: {message}", file=sys.stderr)
    record = logger.makeRecord(logger.name, logging.ERROR, __file__, 0, message, None, None)
    for handler in logging.getLogger().handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.handle(record)


if __name__ == "__main__":
    raise SystemExit(main())
