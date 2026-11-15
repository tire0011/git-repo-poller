"""Command line: check | poll | launch | watch | setup | sync-results."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, installer, launcher, poll, sync_results
from .common import PollerError, default_install_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="git_repo_poller", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="validate a config file; changes nothing")
    p.add_argument("--config", required=True)

    p = sub.add_parser("poll", help="one tick with a given config (no self-update)")
    p.add_argument("--config", required=True)
    p.add_argument("--force", action="store_true", help="run even without a relevant change")

    p = sub.add_parser("launch", help="installed tick: optional self-update, then poll")
    p.add_argument("--install-root", default=str(default_install_root()))
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-update", action="store_true")

    p = sub.add_parser("watch", help="keep running: one launch tick every intervalSeconds (Ctrl+C stops)")
    p.add_argument("--install-root", default=str(default_install_root()))
    p.add_argument("--no-update", action="store_true")

    p = sub.add_parser("setup", help="install / change experiment / repair / pause / resume / uninstall")
    p.add_argument("--install-root", default=str(default_install_root()))
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--config", default="", help="config JSON to install or replace the experiment with (required with --non-interactive)")
    p.add_argument("--repair", action="store_true", help="reinstall wrapper/bootstrap from the installed config")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--pause", action="store_true")
    p.add_argument("--resume", action="store_true")

    p = sub.add_parser("sync-results", help="commit+push allowlisted result paths of one checkout")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--expected-remote", required=True)
    p.add_argument("--result-path", action="append", required=True)
    p.add_argument("--no-commit", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return poll.check(Path(args.config))
        if args.command == "poll":
            return poll.poll(Path(args.config), args.force)
        if args.command == "launch":
            return launcher.launch(Path(args.install_root), args.force, args.no_update)
        if args.command == "watch":
            return launcher.watch(Path(args.install_root), args.no_update)
        if args.command == "setup":
            return installer.setup(
                Path(args.install_root), non_interactive=args.non_interactive or args.repair,
                config_source=args.config, dry_run=args.dry_run,
                uninstall=args.uninstall, pause=args.pause, resume=args.resume)
        if args.command == "sync-results":
            sync_results.sync(Path(args.repo).resolve(), args.branch, args.expected_remote,
                              args.result_path, commit_pending=not args.no_commit)
            return 0
    except PollerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2
