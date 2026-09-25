"""Catch Rate command line: python -m catchrate <command>."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from catchrate.cli import agent, data
from catchrate.cli.common import load_dotenv
from catchrate.repos import REPOS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catchrate", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="where repos, caches and outputs live")
    parser.add_argument("--repo", choices=sorted(REPOS), default="django")
    commands = parser.add_subparsers(dest="command", required=True)
    data.register(commands)
    agent.register(commands)
    args = parser.parse_args(argv)
    load_dotenv(Path(".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)
