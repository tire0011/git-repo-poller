"""Interactive per-user installer. Never runs an experiment during setup.

One experiment per instance: setup asks for exactly one experiment. Running
setup again on an existing installation can replace that experiment, but
never while an execution is active, and never over the previous experiment's
workspace, results or logs (those stay under workspaces/<id>, runs/<id>,
state/<id> keyed by experiment id).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import FileLock, ID_PATTERN, PollerError, git, git_ok, load_config, normalize_experiment, validate_config

DEFAULT_ENGINE_REMOTE = "https://github.com/tire0011/git-repo-poller.git"
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent


class Prompter:
    def __init__(self, interactive: bool):
        self.interactive = interactive

    def ask(self, prompt: str, default: str = "") -> str:
        if not self.interactive:
            return default
        suffix = f" [{default}]" if default else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        return answer or default

    def ask_bool(self, prompt: str, default: bool) -> bool:
        if not self.interactive:
            return default
        answer = input(f"{prompt} {'[Y/n]' if default else '[y/N]'}: ").strip().lower()
        return default if not answer else answer in ("y", "yes", "j", "ja")


def _split(text: str) -> List[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def ask_experiment(ui: Prompter, default_id: str = "demo") -> Dict[str, Any]:
    """Ask for the one experiment this instance runs."""
    if not ui.interactive:
        raise PollerError("For unattended setup, supply the experiment in the --config JSON")
    while True:
        exp_id = ui.ask("Experiment ID (letters, digits, - or _)", default_id)
        if not ID_PATTERN.match(exp_id):
            print("Invalid experiment ID")
            continue
        url = ui.ask("Trusted Git repository URL (anyone who can push to its branch can run code here)")
        if not url:
            print("Git URL cannot be empty")
            continue
        branch = ui.ask("Branch", "main")
        bench = url.rstrip("/").removesuffix(".git").endswith("ai-native-code-bench")
        entry = ui.ask("Entrypoint inside the repo (.ps1 or .py)", "poller-run.ps1" if bench else "run.ps1")
        timeout = int(ui.ask("Timeout in seconds", "600"))
        every_tick = ui.ask_bool("Run on EVERY poll tick, even without new commits?", False)
        defaults = "results/runs/,results/logs/" if bench else ""
        results = ui.ask("Result directories to commit+push before polling (empty = never commit/push)", defaults)
        ignore = ui.ask("Ignore commits that only change these prefixes", defaults)
        return normalize_experiment({
            "id": exp_id, "repo": url, "branch": branch, "entrypoint": entry, "timeoutSeconds": timeout,
            "runMode": "every_tick" if every_tick else "on_change",
            "ignorePaths": _split(ignore), "resultPaths": _split(results),
        })


def _execution_active(root: Path, exp_id: str) -> bool:
    """True while a live process holds the experiment's lock (an execution is running)."""
    lock = FileLock(root / "state" / f"{exp_id}.lock")
    if not lock.acquire():
        return True
    lock.release()
    return False


def _workspace_conflict(root: Path, exp: Dict[str, Any]) -> str:
    """Non-empty when workspaces/<id> already belongs to a different repository."""
    ws = root / "workspaces" / exp["id"]
    if (ws / ".git").exists() and git_ok(ws, "remote", "get-url", "origin"):
        origin = git(ws, "remote", "get-url", "origin")
        if origin != exp["repo"]:
            return f"workspaces/{exp['id']} is already a clone of '{origin}'"
    return ""


def _guard_replacement(root: Path, previous: Optional[Dict[str, Any]], new: Dict[str, Any]) -> None:
    """Never switch experiments during an active execution or onto an existing workspace."""
    if previous is None or new == previous:
        return
    if _execution_active(root, previous["id"]):
        raise PollerError(
            f"Experiment '{previous['id']}' is still running. Stop watch or wait for it to finish "
            "before switching experiments.")
    conflict = _workspace_conflict(root, new)
    if conflict:
        raise PollerError(
            f"{conflict}, but the new experiment wants '{new['repo']}'. Pick a different experiment id "
            "so the previous workspace, results and logs stay untouched.")


def _install_bootstrap(root: Path) -> None:
    target = root / "git_repo_poller"
    tmp = root / ".git_repo_poller.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(PACKAGE_DIR, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)
    shutil.copy2(SOURCE_ROOT / "poller.ps1", root / "poller.ps1")


def _ensure_engine(root: Path, config: Dict[str, Any]) -> None:
    engine = root / "engine"
    if (engine / ".git").exists():
        if git(engine, "remote", "get-url", "origin") != config["engineRemote"]:
            raise PollerError("Existing engine has a different Git remote")
        return
    if engine.exists():
        raise PollerError("Engine directory exists but is not a Git clone")
    proc = subprocess.run(["git", "clone", "--single-branch", "--branch", config["engineBranch"], "--",
                           config["engineRemote"], str(engine)])
    if proc.returncode != 0:
        raise PollerError("Engine clone failed; check Git credentials/network")


def _to_json(config: Dict[str, Any]) -> str:
    return json.dumps(config, indent=2)


def setup(root: Path, *, non_interactive: bool = False, config_source: str = "",
          dry_run: bool = False, uninstall: bool = False, pause: bool = False, resume: bool = False) -> int:
    root = root.resolve()
    installed = root / "config.json"
    ui = Prompter(not non_interactive)
    if pause:
        root.mkdir(parents=True, exist_ok=True)
        (root / "paused.flag").write_text("paused", encoding="ascii")
        print("Poller paused.")
        return 0
    if resume:
        (root / "paused.flag").unlink(missing_ok=True)
        print("Poller resumed.")
        return 0
    if uninstall:
        installed.unlink(missing_ok=True)
        (root / "poller.ps1").unlink(missing_ok=True)
        shutil.rmtree(root / "git_repo_poller", ignore_errors=True)
        print(f"Uninstalled wrapper and config (stop a running watch with Ctrl+C). Workspaces, runs, state and engine kept in {root}")
        return 0
    if not shutil.which("git"):
        raise PollerError("Git is required (Windows: winget install Git.Git)")

    previous: Optional[Dict[str, Any]] = None
    if config_source:
        config = load_config(Path(config_source).resolve())
        if installed.exists():
            previous = load_config(installed)["experiment"]
    elif installed.exists():
        config = load_config(installed)
        previous = config["experiment"]
        if ui.interactive:
            config["workerId"] = ui.ask("Worker ID", config["workerId"])
            config["intervalSeconds"] = int(ui.ask("Check for changes every N seconds",
                                                    str(config["intervalSeconds"])))
            config["autoUpdate"] = ui.ask_bool("Automatically update the poller itself from its Git repository? "
                                               "(every commit there then runs on this machine)", config["autoUpdate"])
            if ui.ask_bool("Replace the configured experiment? (previous workspace, results and logs are kept)",
                           False):
                config["experiment"] = ask_experiment(ui, default_id=previous["id"])
    else:
        if non_interactive:
            raise PollerError("Provide --config with --non-interactive")
        worker = ui.ask("Worker ID (e.g. home-gpu)", platform.node().lower() or "worker")
        config = {
            "workerId": worker,
            "intervalSeconds": int(ui.ask("Check for changes every N seconds", "30")),
            "autoUpdate": ui.ask_bool("Automatically update the poller itself from its Git repository? "
                                      "(every commit there then runs on this machine)", False),
            "engineRemote": DEFAULT_ENGINE_REMOTE, "engineBranch": "main",
            "experiment": ask_experiment(ui),
        }
    if not config["engineRemote"]:
        config["engineRemote"] = DEFAULT_ENGINE_REMOTE
    # Re-validate whatever we are about to write (also migrates legacy single-entry configs).
    tmp_check = validate_config(config)

    print(f"Install folder: {root}")
    job = tmp_check["experiment"]
    print(f"Worker: {tmp_check['workerId']}; interval: {tmp_check['intervalSeconds']} s; "
          f"autoUpdate: {tmp_check['autoUpdate']}")
    print(f"Experiment {job['id']}: {job['repo']} [{job['branch']}] -> {job['entrypoint']} ({job['runMode']})")
    if dry_run:
        print("DRY RUN: configuration checked; nothing cloned, run or written.")
        return 0

    _guard_replacement(root, previous, tmp_check["experiment"])
    root.mkdir(parents=True, exist_ok=True)
    _ensure_engine(root, tmp_check)
    _install_bootstrap(root)
    installed.write_text(_to_json(tmp_check), encoding="utf-8")
    wrapper = root / "poller.ps1"
    print(f"Installed. Config: {installed}")
    print(f'Check:     pwsh -File "{wrapper}" check --config "{installed}"')
    print(f'Start:     pwsh -File "{wrapper}" watch --install-root "{root}"   (checks every '
          f'{tmp_check["intervalSeconds"]} s, Ctrl+C stops)')
    print(f'Run once:  pwsh -File "{wrapper}" launch --install-root "{root}"')
    print(f'Pause:     pwsh -File "{wrapper}" setup --install-root "{root}" --pause')
    print(f'Uninstall: pwsh -File "{wrapper}" setup --install-root "{root}" --uninstall')
    return 0
