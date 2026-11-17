"""Interactive per-user installer. Never runs an experiment during setup."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .common import FileLock, ID_PATTERN, PollerError, git, load_config, normalize_experiment, validate_config

# Where the poller updates itself from; override for forks (and offline tests).
DEFAULT_ENGINE_REMOTE = os.environ.get("GIT_POLLER_ENGINE_REMOTE") or "https://github.com/tire0011/git-repo-poller.git"
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


def _repo_name(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").replace("\\", "/").rsplit("/", 1)[-1]


def _is_bench(url: str) -> bool:
    return _repo_name(url) == "ai-native-code-bench"


def _safe_id(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return cleaned if ID_PATTERN.match(cleaned) else fallback


def choose_experiment(ui: Prompter) -> Dict[str, Any]:
    """Ask the few questions that matter; everything else gets a default.

    Finer settings (ignorePaths, timeoutSeconds, every_tick...) can be edited
    in config.json afterwards. The experiment ID is derived from the repo name.
    """
    if not ui.interactive:
        raise PollerError("For unattended setup, supply one experiment in --config JSON")
    while True:
        url = ui.ask("Git repository URL (anyone who can push there can run code on this machine)")
        if not url:
            print("Git URL cannot be empty")
            continue
        break
    branch = ui.ask("Branch", "main")
    name = _repo_name(url)
    bench = _is_bench(url)
    service = ui.ask_bool("Is it a long-running program (e.g. an agent) that should be restarted "
                          "on every change?", not bench)
    default_entry = "poller-run.ps1" if bench else "start.ps1" if service else "run.ps1"
    while True:
        entry = ui.ask("Program to start, inside the repository (.ps1 or .py)", default_entry)
        if entry.lower().endswith((".ps1", ".py")):
            break
        print("The program must be a .ps1 or .py file")
    results = ""
    if bench:
        results = ui.ask("Result folders to commit+push (empty = never push)", "results/runs/,results/logs/")
    exp_id = _safe_id(name, "experiment")
    job = normalize_experiment({
        "id": exp_id, "repo": url, "branch": branch, "entrypoint": entry,
        "timeoutSeconds": 3600, "runMode": "service" if service else "on_change",
        "ignorePaths": _split(results), "resultPaths": _split(results),
    })
    print(f"Selected {exp_id}: {url} [{branch}] -> {entry} "
          f"({'restart on every change' if service else 'run once per change'})")
    return job


def _ask_interval(ui: Prompter, default: int) -> int:
    while True:
        answer = ui.ask("Check for changes every N seconds", str(default))
        if answer.isdigit() and int(answer) >= 5:
            return int(answer)
        print("Enter a number of seconds (at least 5)")


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


def _setup_unlocked(root: Path, *, non_interactive: bool = False, config_source: str = "", replace: bool = False,
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

    if config_source:
        config = load_config(Path(config_source).resolve())
    elif installed.exists():
        config = load_config(installed)
        # Interactive setup on an existing installation = change the settings.
        replace = replace or not non_interactive
    else:
        if non_interactive:
            raise PollerError("Provide --config with --non-interactive")
        config = {
            "workerId": _safe_id(platform.node().lower(), "worker"),
            "autoUpdate": False,
            "engineRemote": DEFAULT_ENGINE_REMOTE, "engineBranch": "main",
            "experiment": choose_experiment(ui),
        }
        config["intervalSeconds"] = _ask_interval(ui, 30)
    if replace:
        if non_interactive:
            raise PollerError("Use --config JSON for non-interactive experiment replacement")
        if not installed.exists() or config_source:
            raise PollerError("Install first, then use setup --replace-experiment")
        old = config["experiment"]
        print(f"Current: {old['id']}: {old['repo']} [{old['branch']}] -> {old['entrypoint']} ({old['runMode']})")
        new = choose_experiment(ui)
        if old["id"] == new["id"] and (old["repo"], old["branch"]) != (new["repo"], new["branch"]):
            # Same name, different source: never reuse the old workspace/runs.
            n = 2
            while (root / "workspaces" / f"{new['id']}-{n}").exists() or (root / "runs" / f"{new['id']}-{n}").exists():
                n += 1
            new["id"] = f"{new['id']}-{n}"
            print(f"Using experiment ID {new['id']}; files of {old['id']} are kept")
        if old["id"] == new["id"]:
            # Same source: keep settings that are only editable in config.json.
            new["timeoutSeconds"] = old["timeoutSeconds"]
            new["stopTimeoutSeconds"] = old["stopTimeoutSeconds"]
            if not _is_bench(new["repo"]):  # the wizard only asks result folders for the benchmark
                new["ignorePaths"], new["resultPaths"] = old["ignorePaths"], old["resultPaths"]
            if new["runMode"] == "on_change" and old["runMode"] == "every_tick":
                new["runMode"] = "every_tick"
            if new["runMode"] != "service":
                new["verifyEntry"] = old["verifyEntry"]
        config["experiment"] = new
        config["intervalSeconds"] = _ask_interval(ui, config["intervalSeconds"])
    if not config["engineRemote"]:
        config["engineRemote"] = DEFAULT_ENGINE_REMOTE
    # Re-validate whatever we are about to write.
    tmp_check = validate_config(config)

    print(f"Install folder: {root}")
    print(f"Worker: {tmp_check['workerId']}; autoUpdate: {tmp_check['autoUpdate']}")
    job = tmp_check["experiment"]
    print(f"Experiment: {job['id']}: {job['repo']} [{job['branch']}] -> {job['entrypoint']} ({job['runMode']})")
    if dry_run:
        print("DRY RUN: configuration checked; nothing cloned, run or written.")
        return 0

    root.mkdir(parents=True, exist_ok=True)
    _ensure_engine(root, tmp_check)
    _install_bootstrap(root)
    installed.write_text(_to_json(tmp_check), encoding="utf-8")
    wrapper = root / "poller.ps1"
    print(f"Installed. Settings: {installed}")
    print(f'Start:           pwsh -File "{wrapper}"   (Ctrl+C stops)')
    print(f'Change settings: pwsh -File "{wrapper}" setup')
    print(f'Status:          pwsh -File "{wrapper}" status')
    return 0



def setup(root: Path, *, non_interactive: bool = False, config_source: str = "", replace: bool = False,
          dry_run: bool = False, uninstall: bool = False, pause: bool = False, resume: bool = False) -> int:
    """Use the poller's lock so a new experiment cannot replace an active run."""
    args = dict(non_interactive=non_interactive, config_source=config_source, replace=replace,
                dry_run=dry_run, uninstall=uninstall, pause=pause, resume=resume)
    if dry_run or pause or resume:
        # Dry-run must not create even a lock file; pause just prevents future ticks.
        return _setup_unlocked(root, **args)
    lock = FileLock(root.resolve() / "state" / "poller.lock")
    if not lock.acquire():
        raise PollerError("An experiment is currently running; change it after completion")
    try:
        return _setup_unlocked(root, **args)
    finally:
        lock.release()
