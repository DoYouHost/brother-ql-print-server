"""Command line: `python -m label_printer [serve | check | announce install|remove]`."""

import argparse
import sys
from typing import List, Optional

from . import __version__, announce
from .config import ConfigError, load_settings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="label-printer", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the server (the default)")
    commands.add_parser("check", help="validate the settings and report the printer state")
    ann = commands.add_parser("announce", help="write or remove the mDNS announcement (needs root)")
    ann.add_argument("action", choices=["install", "remove"])
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"label-printer: {exc}", file=sys.stderr)
        return 2

    if args.command == "announce":
        announce.install(settings) if args.action == "install" else announce.remove()
        return 0

    if args.command == "check":
        from .server import printer_connected  # imported late: loads OpenCV
        print(f"model {settings.model}, label {settings.label}, printer {settings.identifier}")
        print(f"listening on {settings.host}:{settings.port}")
        print({True: "printer found", False: "printer not found", None: "printer state unknown (not a local device)"}[printer_connected()])
        return 0

    import uvicorn
    from .server import app
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
