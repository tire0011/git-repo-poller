"""Small shared helpers: Git calls, JSON state, non-blocking file locks, config."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
RUN_MODES = ("on_change", "every_tick")


class PollerError(Exception):
    """Expected, user-facing failure (bad config, dirty tree, Git error...)."""


def git(directory: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(directory), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = proc.stdout.strip()
    if check and proc.returncode != 0:
        raise PollerError(f"git {' '.join(args)} failed: {out}")
    return out


def git_ok(directory: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def save_json(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


class FileLock:
    """Non-blocking exclusive lock. The OS releases it if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def _str_list(value: Any) -> List[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def normalize_experiment(job: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one experiment profile and fill defaults."""
    exp_id = str(job.get("id", ""))
    if not ID_PATTERN.match(exp_id):
        raise PollerError(f"Invalid experiment id '{exp_id}'")
    for key in ("repo", "branch", "entrypoint"):
        if not str(job.get(key, "")).strip():
            raise PollerError(f"Experiment '{exp_id}' is missing '{key}'")
    run_mode = job.get("runMode")
    if run_mode is None:
        # Legacy PowerShell config: runOnChange=false meant "run every tick".
        run_mode = "every_tick" if job.get("runOnChange") is False else "on_change"
    if run_mode not in RUN_MODES:
        raise PollerError(f"Experiment '{exp_id}': runMode must be one of {RUN_MODES}")
    timeout = int(job.get("timeoutSeconds") or 600)
    return {
        "id": exp_id,
        "repo": str(job["repo"]).strip(),
        "branch": str(job["branch"]).strip(),
        "entrypoint": str(job["entrypoint"]).strip(),
        "verifyEntry": str(job.get("verifyEntry") or "").strip() or None,
        "timeoutSeconds": max(1, timeout),
        "runMode": run_mode,
        "ignorePaths": _str_list(job.get("ignorePaths")),
        "resultPaths": _str_list(job.get("resultPaths")),
    }


def _interval_seconds(raw: Dict[str, Any]) -> int:
    if raw.get("intervalSeconds"):
        return max(5, int(raw["intervalSeconds"]))
    if raw.get("intervalMinutes"):  # older configs
        return max(5, int(raw["intervalMinutes"]) * 60)
    return 30


def _single_experiment(raw: Dict[str, Any]) -> Dict[str, Any]:
    """One experiment per instance. Legacy 'experiments' lists migrate if they hold exactly one entry."""
    if raw.get("experiment") is not None:
        if raw.get("experiments"):
            raise PollerError("Configuration has both 'experiment' and 'experiments'; use only 'experiment'")
        return normalize_experiment(raw["experiment"])
    legacy = raw.get("experiments") or []
    if len(legacy) == 1:
        return normalize_experiment(legacy[0])
    if len(legacy) > 1:
        raise PollerError(
            f"This poller runs exactly ONE experiment per instance, but the configuration lists {len(legacy)}. "
            "Split them into separate installations: one install folder and one config per experiment "
            "(see README 'Multiple workers / experiments').")
    raise PollerError("Configuration must define one 'experiment' object")


def load_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise PollerError(f"Configuration not found: {path}")
    return validate_config(load_json(path))


def validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    worker = str(raw.get("workerId", ""))
    if not ID_PATTERN.match(worker):
        raise PollerError(f"Invalid workerId '{worker}'")
    return {
        "workerId": worker,
        "intervalSeconds": _interval_seconds(raw),
        # Self-update is opt-in: every engine commit is code that runs as you.
        "autoUpdate": raw.get("autoUpdate") is True,
        "engineRemote": str(raw.get("engineRemote") or "").strip(),
        "engineBranch": str(raw.get("engineBranch") or "main").strip(),
        "experiment": _single_experiment(raw),
    }


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def matches_prefix(path: str, prefixes: List[str]) -> bool:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    for prefix in prefixes:
        pre = prefix.replace("\\", "/").strip("/") + "/"
        if p.startswith(pre):
            return True
    return False


def default_install_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "GitRepoPoller"
