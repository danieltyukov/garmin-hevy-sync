"""Orchestrator for all four sync flows.

Flow order is load-bearing. A runs before B so that Hevy workouts reach Garmin
first, merging into whatever the watch recorded. By the time B looks at Garmin
activities, anything that came from Hevy already has a Hevy counterpart within
the overlap window and is skipped. Running B first would import a watch session
into Hevy that A was about to enrich, producing a duplicate.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from . import state
from .config import (
    GARMIN_TOKENS,
    EXERCISE_MAP_FILE,
    LOG_DIR,
    Settings,
    ensure_dirs,
    hevy2garmin_binary,
)
from .flows import flow_b_garmin_to_hevy, flow_d_body_measurements
from .garmin_client import connect
from .hevy import HevyClient

logger = logging.getLogger("gh_sync")


def setup_logging(verbose: bool) -> None:
    ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # Rotate rather than append forever: the unit runs every 30 minutes and
    # writes ~25 lines a run, so a plain FileHandler grows without bound on a
    # box nothing else prunes. Five 5 MB generations is roughly a year of
    # history at that rate.
    handlers.append(
        RotatingFileHandler(
            LOG_DIR / "sync.log", maxBytes=5 * 1024 * 1024, backupCount=5
        )
    )
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # These are chatty at DEBUG and drown out our own lines.
    for noisy in ("urllib3", "garth", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run_hevy2garmin(args: list[str], dry_run: bool) -> bool:
    """Shell out to the hevy2garmin CLI. Returns True on success."""
    binary = hevy2garmin_binary()
    if not binary:
        logger.error("hevy2garmin is not installed in this venv")
        return False
    command = [binary, *args]
    if dry_run and "--dry-run" not in command:
        command.append("--dry-run")
    logger.info("Running %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    for line in (result.stdout or "").splitlines():
        logger.info("[hevy2garmin] %s", line)
    for line in (result.stderr or "").splitlines():
        logger.warning("[hevy2garmin] %s", line)
    if result.returncode != 0:
        logger.error("hevy2garmin %s exited %s", args[0], result.returncode)
        return False
    return True


def flow_a(dry_run: bool) -> bool:
    """Hevy workouts -> Garmin activities (merging into watch recordings)."""
    return _run_hevy2garmin(["sync"], dry_run)


def flow_c(dry_run: bool) -> bool:
    """Hevy routines -> Garmin planned workouts."""
    return _run_hevy2garmin(["sync-routines"], dry_run)


def cmd_sync(args: argparse.Namespace) -> int:
    settings = Settings.from_env(dry_run=args.dry_run)
    ensure_dirs()

    selected = set(args.flows) if args.flows else {"a", "b", "c", "d"}
    summary: dict[str, object] = {}
    failures = 0

    if "a" in selected:
        summary["a_hevy_to_garmin"] = "ok" if flow_a(args.dry_run) else "failed"
        failures += summary["a_hevy_to_garmin"] == "failed"
    if "c" in selected:
        summary["c_routines_to_garmin"] = "ok" if flow_c(args.dry_run) else "failed"
        failures += summary["c_routines_to_garmin"] == "failed"

    if selected & {"b", "d"}:
        hevy = HevyClient(settings.hevy_api_key)
        garmin = connect(settings.garmin_email, settings.garmin_password)
        with state.connect() as conn:
            run_id = state.start_run(conn)
            if "b" in selected:
                summary["b_garmin_to_hevy"] = flow_b_garmin_to_hevy(
                    garmin, hevy, conn, settings
                )
            if "d" in selected:
                summary["d_body_measurements"] = flow_d_body_measurements(
                    garmin, hevy, conn, settings
                )
            state.finish_run(conn, run_id, json.dumps(summary, default=str))

    logger.info("Sync summary: %s", json.dumps(summary, default=str))
    return 1 if failures else 0


def cmd_status(_: argparse.Namespace) -> int:
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM garmin_to_hevy GROUP BY status"
        ).fetchall()
        print("Flow B (Garmin -> Hevy):")
        if not rows:
            print("  nothing recorded yet")
        for row in rows:
            print(f"  {row['status']:<9} {row['n']}")

        measurements = conn.execute("SELECT COUNT(*) AS n FROM body_measurements").fetchone()
        print(f"\nFlow D body measurements synced: {measurements['n']}")

        last = conn.execute(
            "SELECT started_at, ended_at, summary FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            print(f"\nLast run: {last['started_at']} -> {last['ended_at'] or 'incomplete'}")
            if last["summary"]:
                print(f"  {last['summary']}")

        recent = conn.execute(
            "SELECT garmin_activity_id, status, hevy_workout_id, note "
            "FROM garmin_to_hevy ORDER BY synced_at DESC LIMIT 10"
        ).fetchall()
        if recent:
            print("\nMost recent activities considered:")
            for row in recent:
                detail = row["hevy_workout_id"] or row["note"] or ""
                print(f"  {row['garmin_activity_id']:<14} {row['status']:<9} {detail}")
    return 0


def cmd_unmapped(_: argparse.Namespace) -> int:
    if not EXERCISE_MAP_FILE.exists():
        print(f"No map yet at {EXERCISE_MAP_FILE}. Run a sync first.")
        return 0
    data = json.loads(EXERCISE_MAP_FILE.read_text())
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
    print(f"\nEdit {EXERCISE_MAP_FILE}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Verify both credentials work and report what each side sees."""
    ok = True
    settings = Settings.from_env()

    print("Hevy:")
    try:
        hevy = HevyClient(settings.hevy_api_key)
        # /v1/user/info wraps the user under a "data" key.
        info = hevy.user_info()
        user = info.get("data", info)
        count = hevy.workout_count()
        print(f"  connected as {user.get('name') or user.get('username') or '?'}")
        print(f"  {count} workouts on the account")
        templates = list(hevy.iter_exercise_templates())
        routines = list(hevy.iter_routines())
        print(f"  {len(templates)} exercise templates, {len(routines)} routines")
        if count == 0:
            print("  NOTE: no workouts yet, so flows A and B have nothing to move")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        ok = False

    print("\nGarmin:")
    try:
        garmin = connect(settings.garmin_email, settings.garmin_password)
        name = garmin.get_full_name()
        devices = garmin.get_devices() or []
        print(f"  connected as {name}")
        for device in devices:
            print(f"  device: {device.get('displayName') or device.get('productDisplayName')}")
        from .garmin_client import strength_activities

        activities = strength_activities(garmin, settings.lookback_days)
        print(f"  {len(activities)} strength activities in the last {settings.lookback_days} days")
        for activity in activities[-5:]:
            print(
                f"    {activity.get('startTimeGMT')}  {activity.get('activityName')} "
                f"(id {activity.get('activityId')})"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        ok = False

    print("\nhevy2garmin:")
    binary = hevy2garmin_binary()
    if binary:
        print(f"  {binary}")
    else:
        print("  MISSING (flows A and C will not run)")
        ok = False

    return 0 if ok else 1


def cmd_login(_: argparse.Namespace) -> int:
    """One-time interactive Garmin sign-in, including the MFA code.

    Everything afterwards runs off the cached tokens in ~/.garminconnect, which
    refresh themselves, so this should only ever need running once.
    """
    settings = Settings.from_env()
    print(f"Signing in to Garmin Connect as {settings.garmin_email}")
    try:
        garmin = connect(settings.garmin_email, settings.garmin_password, interactive=True)
    except Exception as exc:  # noqa: BLE001
        print(f"\nLogin failed: {exc}")
        return 1
    print(f"\nSigned in as {garmin.get_full_name()}")
    print(f"Tokens cached in {GARMIN_TOKENS}; future runs need no password prompt.")
    return 0


def cmd_push_to_watch(args: argparse.Namespace) -> int:
    """Push Garmin planned workouts to the watch immediately.

    Garmin normally delivers workouts on its own schedule; this forces them
    across now. Deliberately not part of the periodic sync, which would re-push
    the same workouts every 30 minutes.
    """
    settings = Settings.from_env()
    garmin = connect(settings.garmin_email, settings.garmin_password)
    devices = garmin.get_devices() or []
    if not devices:
        print("No Garmin devices found on the account.")
        return 1
    device_id = devices[0].get("deviceId")
    print(f"Pushing to {devices[0].get('displayName') or device_id}")

    workouts = garmin.get_workouts(0, args.limit) or []
    pushed = 0
    for workout in workouts:
        workout_id = workout.get("workoutId")
        try:
            garmin.push_workout_to_device(workout_id, device_id)
            print(f"  pushed {workout.get('workoutName')}")
            pushed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  failed {workout.get('workoutName')}: {exc}")
    print(f"{pushed}/{len(workouts)} workouts pushed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gh-sync", description="Two-way sync between a Garmin watch and Hevy"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="run the sync flows (default: all four)")
    p_sync.add_argument(
        "--flows", nargs="+", choices=["a", "b", "c", "d"],
        help="a=Hevy->Garmin  b=Garmin->Hevy  c=routines->Garmin  d=body measurements",
    )
    p_sync.add_argument("--dry-run", action="store_true", help="report without writing")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("status", help="show the sync ledger").set_defaults(func=cmd_status)
    sub.add_parser("unmapped", help="Garmin exercises with no Hevy match").set_defaults(
        func=cmd_unmapped
    )
    sub.add_parser("doctor", help="verify credentials and connectivity").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("login", help="one-time interactive Garmin sign-in (handles MFA)").set_defaults(
        func=cmd_login
    )

    p_push = sub.add_parser("push-to-watch", help="force planned workouts onto the watch")
    p_push.add_argument("--limit", type=int, default=25)
    p_push.set_defaults(func=cmd_push_to_watch)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    started = datetime.now(timezone.utc)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    finally:
        if args.command == "sync":
            logger.info(
                "Finished in %.1fs", (datetime.now(timezone.utc) - started).total_seconds()
            )


if __name__ == "__main__":
    raise SystemExit(main())
