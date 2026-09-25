"""Daytrace hub command line.

DT-1 skeleton. The subcommands are implemented by DT-10 (run), DT-15 (seed) and DT-16 (tracker).
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import PROFILES


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
    print(f"'{args.command}' is not implemented yet. See its ticket.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
