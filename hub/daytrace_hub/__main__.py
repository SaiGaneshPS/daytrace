"""Daytrace hub command line.

`run` starts a profile (DT-10), `seed` fills a demo profile (DT-15), `tracker` runs only the desktop tracker (DT-16),
`demo` sends what the demo phone would send now (DT-48).
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import PROFILES, load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daytrace-hub",
        description="Daytrace hub: your local, cross-device activity hub.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    run = sub.add_parser("run", help="start the hub (DT-10)")
    run.add_argument("--profile", choices=PROFILES, default="personal")

    seed = sub.add_parser("seed", help="fill a demo profile with 14 days of believable data (DT-15)")
    seed.add_argument("--profile", choices=PROFILES, default="demo")
    seed.add_argument("--days", type=int, default=14)
    seed.add_argument("--tz", default=None, help="IANA time zone for the demo days (default: this computer's)")

    tracker = sub.add_parser("tracker", help="run only the desktop activity tracker (DT-16)")
    tracker.add_argument("--profile", choices=PROFILES, default="personal")

    demo = sub.add_parser("demo", help="send what the demo phone would send now: live apps, or a nudge (DT-48)")
    demo.add_argument("scenario", choices=("live", "nudge"), help="live: apps ending now; nudge: TikTok during a study block")
    demo.add_argument("--profile", choices=PROFILES, default="demo")
    demo.add_argument("--again", action="store_true", help="let a nudge speak now even if one went out in the last minutes")
    demo.add_argument("--no-toast", action="store_true", help="don't show the nudge as a desktop notification too")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "run":
        return run(args.profile)
    if args.command == "seed":
        return seed(args.profile, args.days, args.tz)
    if args.command == "tracker":
        return tracker(args.profile)
    if args.command == "demo":
        return demo(args.profile, args.scenario, args.again, not args.no_toast)
    print(f"'{args.command}' is not implemented yet. See its ticket.", file=sys.stderr)
    return 2


def tracker(profile_name: str) -> int:
    """Run only the desktop tracker, in the foreground (the hub also runs it itself for the personal profile)."""
    from .app import desktop_tracker
    from .db import Database
    from .tracker.base import AlreadyTracking

    try:
        settings = load_settings(profile_name)
    except ValueError as error:
        print(f"Not tracking: {error}", file=sys.stderr)
        return 2
    if not settings.track_desktop:
        print(f"Not tracking: DAYTRACE_TRACKER is off for the {profile_name} profile (set it to on to track this"
              " computer there)", file=sys.stderr)
        return 2
    database = Database(settings.database_path)
    database.initialize()
    service = desktop_tracker(settings, database)
    if service is None:
        print(f"Not tracking: there is no desktop tracker for {sys.platform} yet (macOS arrives with DT-17)", file=sys.stderr)
        return 2
    print(f"Daytrace tracker: {profile_name} profile, every 2 s. Press Ctrl+C to stop.")
    print(f"  Database: {settings.database_path}")
    try:
        service.run_forever()
    except AlreadyTracking as error:
        print(f"Not tracking: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    print(f"Stopped. Device: {service.device_id}")
    return 0


def seed(profile_name: str, days: int, tz_name: str | None) -> int:
    """Replace a demo profile's seed data. Profiles with real data (personal) are refused.

    Expected problems get a one-line message and exit code 2; anything else is a bug and keeps its traceback.
    """
    import sqlite3
    from zoneinfo import ZoneInfo

    from .api.timeline import known_zones, local_zone_name
    from .seed import SeedRefused
    from .seed import seed as fill

    zone_name = tz_name or local_zone_name()
    if zone_name not in known_zones():
        return _not_seeded(f"unknown time zone {zone_name!r}; use an IANA name such as America/Toronto")
    try:
        settings = load_settings(profile_name)
    except ValueError as error:  # a bad DAYTRACE_* environment variable
        return _not_seeded(str(error))
    try:
        result = fill(settings, days, ZoneInfo(zone_name))
    except SeedRefused as error:
        return _not_seeded(str(error))
    except sqlite3.OperationalError as error:
        return _not_seeded(f"the {profile_name} database is busy or unreadable ({error}); stop its hub and retry")
    except RuntimeError as error:  # a database from a newer hub, or seed data the hub refused
        return _not_seeded(str(error))
    print(f"Seeded the {profile_name} profile: {result.first_day} to {result.last_day} ({zone_name}),"
          f" {result.total} events (the first night's sleep starts the evening before)")
    for device_id, count in result.events.items():
        print(f"  {device_id}: {count}")
    print(f"  Database: {settings.database_path}")
    return 0


def demo(profile_name: str, scenario: str, again: bool, toast: bool) -> int:
    """What the demo phone would send now (DT-48), with the nudge the hub answered, shown on this computer too."""
    import sqlite3

    from . import notify
    from .seed import demo_events

    try:
        settings = load_settings(profile_name)
        result = demo_events(settings, "nudge" if scenario == "nudge" else "live", again=again)
    except (ValueError, RuntimeError) as error:  # SeedRefused is a ValueError
        return _not_sent(str(error))
    except sqlite3.OperationalError as error:
        return _not_sent(f"the {profile_name} database is busy or unreadable ({error})")
    print(result.sent)
    if scenario == "nudge":
        if result.nudge is None:
            print("No nudge: one went out in the last few minutes (the hub sends one at a time). Run it again with --again.")
            return 1
        print(f"Nudge: {result.nudge.title}. {result.nudge.body}")
        if toast and not notify.show(result.nudge.title, result.nudge.body, wait=True):
            print("(The desktop notification couldn't be shown here.)")
    return 0


def _not_sent(message: str) -> int:
    print(f"Not sent: {message}", file=sys.stderr)
    return 2


def _not_seeded(message: str) -> int:
    print(f"Not seeded: {message}", file=sys.stderr)
    return 2


def run(profile_name: str) -> int:
    """Start one profile's hub on its port, with its own database, listening only where it is reached (DT-45)."""
    from .app import serve

    settings = load_settings(profile_name)
    profile = settings.profile
    print(f"Daytrace hub: {profile.name} profile on port {profile.port}")
    print(f"  {profile.description}")
    print(f"  Database: {settings.database_path}")
    serve(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
