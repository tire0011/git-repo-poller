"""Stable launcher: optional engine self-update between ticks, then one poll tick.

Install layout (default %LOCALAPPDATA%\\GitRepoPoller):
  config.json, poller.ps1, git_repo_poller/   stable bootstrap copy (this code)
  engine/                                    Git clone of the poller repository
  versions/<sha>/git_repo_poller/            immutable snapshots used for ticks
  current-version.txt                        last selected (known good) snapshot
  workspaces/, state/, runs/                 experiment checkouts, status, run outputs
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List

from .common import FileLock, PollerError, git, load_config


def _python_in(snapshot: Path, args: List[str]) -> int:
    env = dict(os.environ, PYTHONPATH=str(snapshot))
    proc = subprocess.Popen([sys.executable, "-m", "git_repo_poller", *args], cwd=str(snapshot), env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Ctrl+C reached the tick as well. Unlike subprocess.run, do not kill it:
        # a service-mode tick must stop its supervised process cleanly first.
        proc.wait()
        raise


def _update_engine(engine: Path, remote: str, branch: str) -> None:
    actual = git(engine, "remote", "get-url", "origin")
    if actual != remote:
        raise PollerError("Engine origin differs from configured engineRemote. Update refused.")
    if git(engine, "status", "--porcelain", "--untracked-files=all"):
        raise PollerError("Engine checkout is dirty. Refusing self-update.")
    try:
        git(engine, "fetch", "origin", branch, "--quiet")
    except PollerError:
        print("WARNING: engine fetch failed (offline?); keeping installed version", file=sys.stderr)
        return
    try:
        git(engine, "merge", "--ff-only", f"origin/{branch}")
    except PollerError:
        print("WARNING: engine cannot fast-forward; keeping last known good version", file=sys.stderr)


def _snapshot(engine: Path, versions: Path, sha: str) -> Path:
    target = versions / sha
    if not (target / "git_repo_poller").is_dir():
        source = engine / "git_repo_poller"
        if not source.is_dir():
            raise PollerError(f"Engine revision {sha[:8]} has no git_repo_poller package")
        tmp = versions / f".{sha}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(source, tmp / "git_repo_poller",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.replace(tmp, target)
    return target


def select_engine(root: Path, config: dict, no_update: bool) -> Path:
    engine = root / "engine"
    versions = root / "versions"
    selected_file = root / "current-version.txt"
    if not (engine / ".git").exists():
        if not config["autoUpdate"] and (root / "git_repo_poller").is_dir():
            # Package-based installation with self-update disabled: the
            # installed bootstrap copy is the engine; no poller clone needed.
            return root
        raise PollerError("Engine checkout missing; run 'setup --repair'")
    versions.mkdir(parents=True, exist_ok=True)
    old = selected_file.read_text(encoding="ascii").strip() if selected_file.exists() else ""
    if config["autoUpdate"] and not no_update:
        _update_engine(engine, config["engineRemote"], config["engineBranch"])
    candidate = git(engine, "rev-parse", "HEAD")
    if candidate != old:
        try:
            snap = _snapshot(engine, versions, candidate)
            # Startup + config check only. NOT a security gate: this already
            # executes the new code, so only trust engine remotes you control.
            ok = _python_in(snap, ["check", "--config", str(root / "config.json")]) == 0
        except PollerError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)
            ok = False
        if ok:
            selected_file.write_text(candidate, encoding="ascii")
            return versions / candidate
        if not old:
            # launch() already loaded the config with this bootstrap. Keep a
            # newer local install usable when the cloned engine is older (or
            # otherwise incompatible) and no snapshot has been selected yet.
            if (root / "git_repo_poller").is_dir():
                print("WARNING: no compatible engine snapshot; using the installed bootstrap", file=sys.stderr)
                return root
            raise PollerError("No valid engine version available")
        print(f"WARNING: engine candidate {candidate[:8]} failed its check; keeping {old[:8]}", file=sys.stderr)
    snap = versions / old
    if not (snap / "git_repo_poller").is_dir():
        raise PollerError(f"Selected engine snapshot is missing: {snap}")
    return snap


def launch(root: Path, force: bool = False, no_update: bool = False) -> int:
    root = root.resolve()
    config_path = root / "config.json"
    if not config_path.exists():
        raise PollerError(f"Run setup first: {config_path} not found")
    if (root / "paused.flag").exists():
        print("Poller is paused.")
        return 0
    config = load_config(config_path)
    lock = FileLock(root / "state" / "launcher.lock")
    if not lock.acquire():
        print("Another launcher is updating the engine. Skipping tick.")
        return 0
    try:
        snap = select_engine(root, config, no_update)
    finally:
        # Ticks lock per experiment; the launcher lock only guards engine selection.
        lock.release()
    print(f"Engine: {snap.name[:12] if snap != root else 'installed bootstrap'}")
    args = ["poll", "--config", str(config_path)]
    if force:
        args.append("--force")
    return _python_in(snap, args)



def watch(root: Path, no_update: bool = False) -> int:
    """Long-running mode: a launch tick, then sleep intervalSeconds, until Ctrl+C."""
    root = root.resolve()
    print(f"Watching {root} (Ctrl+C stops)", flush=True)
    try:
        while True:
            interval = 30
            try:
                interval = load_config(root / "config.json")["intervalSeconds"]
                launch(root, no_update=no_update)
            except PollerError as exc:
                # Keep running: offline, a broken config edit etc. may be fixed meanwhile.
                print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
