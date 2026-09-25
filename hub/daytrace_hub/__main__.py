"""Daytrace hub command line.

`run` starts a profile (DT-10); `seed` (DT-15) and `tracker` (DT-16) are filled in by their tickets.
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

    seed = sub.add_parser("seed", help="generate demo data (DT-15)")
    seed.add_argument("--profile", choices=PROFILES, default="demo")
    seed.add_argument("--days", type=int, default=14)

    tracker = sub.add_parser("tracker", help="run only the desktop activity tracker (DT-16)")
    tracker.add_argument("--profile", choices=PROFILES, default="personal")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "run":
        return run(args.profile)
    print(f"'{args.command}' is not implemented yet. See its ticket.", file=sys.stderr)
    return 2


def run(profile_name: str) -> int:
    """Start one profile's hub on its port, with its own database."""
    import uvicorn

    from .app import create_app

    settings = load_settings(profile_name)
    profile = settings.profile
    print(f"Daytrace hub: {profile.name} profile on port {profile.port}")
    print(f"  {profile.description}")
    print(f"  Database: {settings.database_path}")
    # proxy_headers=False: the network check must see the real peer address, never an X-Forwarded-For value.
    uvicorn.run(
        create_app(settings), host=profile.host, port=profile.port, log_level="info", proxy_headers=False
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
