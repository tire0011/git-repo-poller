"""Watch one Git repository; pull changes and (re)start its program.

Run without a command to start (the first run asks a few setup questions).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, installer, launcher, poll, sync_results
from .common import PollerError, default_install_root, load_config, load_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="git-repo-poller", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    # Only start/setup/status are listed; the other commands are plumbing used
    # internally (launch/poll/check), by tests and by advanced setups.
    sub = parser.add_subparsers(dest="command", metavar="{start,setup,status}")

    p = sub.add_parser("start", help="(default) set up if needed, then watch and run until Ctrl+C")
    p.add_argument("--install-root", default=str(default_install_root()))

    p = sub.add_parser("status", help="show what is running and the last result")
    p.add_argument("--install-root", default=str(default_install_root()))

    p = sub.add_parser("check")
    p.add_argument("--config", required=True)

    p = sub.add_parser("poll")
    p.add_argument("--config", required=True)
    p.add_argument("--force", action="store_true", help="run even without a relevant change")

    p = sub.add_parser("launch")
    p.add_argument("--install-root", default=str(default_install_root()))
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-update", action="store_true")

    p = sub.add_parser("watch")
    p.add_argument("--install-root", default=str(default_install_root()))
    p.add_argument("--no-update", action="store_true")

    p = sub.add_parser("setup", help="change settings (also --pause, --resume, --uninstall, --repair)")
    p.add_argument("--install-root", default=str(default_install_root()))
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--config", default="", help="config JSON to install (required with --non-interactive)")
    p.add_argument("--replace-experiment", action="store_true", help="switch to another experiment while retaining previous files")
    p.add_argument("--repair", action="store_true", help="reinstall wrapper/bootstrap from the installed config")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--pause", action="store_true")
    p.add_argument("--resume", action="store_true")

    p = sub.add_parser("sync-results")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--expected-remote", required=True)
    p.add_argument("--result-path", action="append", required=True)
    p.add_argument("--no-commit", action="store_true")
    return parser


def start(root: Path) -> int:
    root = root.resolve()
    if not (root / "config.json").exists():
        print("First start: a few questions, then the poller runs. (Enter accepts [defaults].)")
        if installer.setup(root) != 0:
            return 1
        if not installer.Prompter(True).ask_bool("Start now?", True):
            return 0
    paused = root / "paused.flag"
    if paused.exists():
        paused.unlink()
        print("Was paused; resumed.")
    return launcher.watch(root)


def status(root: Path) -> int:
    root = root.resolve()
    config_path = root / "config.json"
    if not config_path.exists():
        print(f"Not set up yet ({root}). Run git-repo-poller (or poller.ps1) without arguments to start.")
        return 1
    job = load_config(config_path)["experiment"]
    mode = "restart on every change" if job["runMode"] == "service" else job["runMode"]
    print(f"Repository: {job['repo']} [{job['branch']}] -> {job['entrypoint']} ({mode})")
    if (root / "paused.flag").exists():
        print("Paused.")
    state_file = root / "state" / f"{job['id']}.json"
    if not state_file.exists():
        print("Has not run yet.")
        return 0
    st = load_json(state_file)
    print(f"Status:     {st.get('status')} at commit {str(st.get('commitSha') or '-')[:8]}"
          + (f", pid {st['pid']}" if st.get("pid") else "")
          + (f", exit code {st['exitCode']}" if st.get("exitCode") is not None else ""))
    print(f"Started:    {st.get('startedAt') or '-'}   Finished: {st.get('finishedAt') or '-'}")
    if st.get("error"):
        print(f"Problem:    {st['error']}")
    if st.get("stdoutLog"):
        print(f"Output:     {st['stdoutLog']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["start"])
    try:
        if args.command == "start":
            return start(Path(args.install_root))
        if args.command == "status":
            return status(Path(args.install_root))
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
                config_source=args.config, replace=args.replace_experiment, dry_run=args.dry_run,
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
