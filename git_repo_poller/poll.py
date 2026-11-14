"""One poll tick: update approved checkouts and run changed experiments.

Each experiment is handled in its own thread with its own lock, so a long
experiment never blocks the others and overlapping ticks skip busy ones.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from . import sync_results
from .common import FileLock, PollerError, git, is_under, load_config, load_json, matches_prefix, save_json

FAILED_STATES = {"error", "failed", "timed_out", "verification_failed"}
_print_lock = threading.Lock()


def _say(text: str) -> None:
    with _print_lock:
        print(text, flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def entry_command(script: Path) -> List[str]:
    suffix = script.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(script)]
    if suffix == ".ps1":
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise PollerError("PowerShell 7 (pwsh) is required for .ps1 entrypoints")
        return [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)]
    raise PollerError(f"Entrypoint must be a .ps1 or .py file: {script.name}")


def resolve_entry(checkout: Path, relative: str) -> Path:
    script = (checkout / relative).resolve()
    if not is_under(script, checkout) or not script.is_file():
        raise PollerError(f"Entrypoint must be an existing file inside the approved checkout: {relative}")
    entry_command(script)  # validates the file type
    return script


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_entry(script: Path, cwd: Path, timeout: int, run_dir: Path, name: str,
              env: Dict[str, str]) -> Dict[str, Any]:
    stdout_log = run_dir / f"{name}.out.log"
    stderr_log = run_dir / f"{name}.err.log"
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(stdout_log, "wb") as out, open(stderr_log, "wb") as err:
        proc = subprocess.Popen(entry_command(script), cwd=str(cwd), stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, env=env, **kwargs)
        try:
            code = proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            code, timed_out = 124, True
    return {"exitCode": code, "timedOut": timed_out,
            "stdoutLog": str(stdout_log), "stderrLog": str(stderr_log)}


def _ensure_checkout(job: Dict[str, Any], checkout: Path) -> None:
    if not (checkout / ".git").exists():
        if checkout.exists():
            raise PollerError(f"Path exists but is not a Git clone: {checkout}")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--single-branch", "--branch", job["branch"], "--", job["repo"], str(checkout)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        if proc.returncode != 0:
            raise PollerError(f"Clone failed: {proc.stdout.strip()}")
    origin = git(checkout, "remote", "get-url", "origin")
    if origin != job["repo"]:
        raise PollerError(f"Wrong Git remote '{origin}' (expected '{job['repo']}')")
    branch = git(checkout, "branch", "--show-current")
    if branch != job["branch"]:
        raise PollerError(f"Wrong branch '{branch}' (expected '{job['branch']}')")


def _update_checkout(job: Dict[str, Any], checkout: Path) -> str:
    if job["resultPaths"]:
        # Opt-in: commit/publish pending RESULT-ONLY changes before updating.
        sync_results.sync(checkout, job["branch"], job["repo"], job["resultPaths"], commit_pending=True,
                          log=lambda m: _say(f"{job['id']}: {m}"))
    if git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise PollerError(
            "Dirty workspace; refusing to update or discard local changes. "
            "Experiments should write outputs to $GIT_POLLER_RUN_DIR (outside the checkout) "
            "or declare them as resultPaths.")
    git(checkout, "fetch", "origin", job["branch"], "--quiet")
    try:
        git(checkout, "merge", "--ff-only", f"origin/{job['branch']}")
    except PollerError as exc:
        raise PollerError(f"Checkout cannot fast-forward (diverged?); left untouched. {exc}") from None
    return git(checkout, "rev-parse", "HEAD")


def _relevant_change(job: Dict[str, Any], checkout: Path, previous: str, sha: str) -> bool:
    changed = [p for p in git(checkout, "diff", "--name-only", previous, sha, "--").splitlines() if p]
    ignored = job["ignorePaths"] + job["resultPaths"]
    return any(not matches_prefix(p, ignored) for p in changed)


def poll_experiment(job: Dict[str, Any], root: Path, worker_id: str, force: bool) -> Dict[str, Any]:
    exp_id = job["id"]
    states = root / "state"
    state_file = states / f"{exp_id}.json"
    lock = FileLock(states / f"{exp_id}.lock")
    if not lock.acquire():
        _say(f"{exp_id}: busy (still running from an earlier tick); skipped")
        return {"experimentId": exp_id, "status": "busy"}
    try:
        state = load_json(state_file) if state_file.exists() else {}
        previous = str(state.get("lastSeenSha") or "")
        record: Dict[str, Any] = {
            "experimentId": exp_id, "workerId": worker_id, "lastSeenSha": previous,
            "commitSha": "", "status": "checking", "startedAt": None, "finishedAt": None,
            "exitCode": None, "verificationStatus": "not_configured", "runDir": None,
            "stdoutLog": None, "stderrLog": None, "error": None,
        }
        # We hold the lock, so a stored "running" means the previous run died
        # (reboot, crash). Make that visible instead of silently moving on.
        interrupted = state.get("status") in ("running", "interrupted")
        try:
            checkout = root / "workspaces" / exp_id
            _ensure_checkout(job, checkout)
            sha = _update_checkout(job, checkout)
            record["commitSha"] = sha
            if force or not previous or job["runMode"] == "every_tick":
                should_run = True
            elif previous == sha:
                should_run = False
            else:
                should_run = _relevant_change(job, checkout, previous, sha)
            if not should_run:
                if interrupted and previous == sha:
                    record["status"] = "interrupted"
                    record["error"] = "Previous run was interrupted; use --force to retry"
                else:
                    record["status"] = "unchanged" if previous == sha else "ignored_change"
                record["lastSeenSha"] = sha
                return record
            script = resolve_entry(checkout, job["entrypoint"])
            verify = resolve_entry(checkout, job["verifyEntry"]) if job["verifyEntry"] else None
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            run_dir = root / "runs" / exp_id / f"{stamp}-{sha[:8]}"
            n = 1
            while run_dir.exists():  # never share a run dir between two runs
                n += 1
                run_dir = root / "runs" / exp_id / f"{stamp}-{sha[:8]}-{n}"
            run_dir.mkdir(parents=True)
            env = dict(os.environ, GIT_POLLER_RUN_DIR=str(run_dir), GIT_POLLER_EXPERIMENT_ID=exp_id,
                       GIT_POLLER_COMMIT=sha, GIT_POLLER_WORKER_ID=worker_id)
            # Persist the attempted commit before starting: a failing experiment
            # is not restarted every tick. Retry needs a new commit or --force.
            record.update(lastSeenSha=sha, status="running", startedAt=_now(), runDir=str(run_dir))
            save_json(state_file, record)
            result = run_entry(script, checkout, job["timeoutSeconds"], run_dir, "run", env)
            record.update(exitCode=result["exitCode"], stdoutLog=result["stdoutLog"],
                          stderrLog=result["stderrLog"])
            if result["timedOut"]:
                record["status"] = "timed_out"
            elif result["exitCode"] != 0:
                record["status"] = "failed"
            else:
                # exit 0 only means "executed"; validity needs verifyEntry.
                record["status"] = "succeeded"
                if verify:
                    try:
                        vr = run_entry(verify, checkout, job["timeoutSeconds"], run_dir, "verify", env)
                        record["verificationStatus"] = (
                            "timed_out" if vr["timedOut"] else "passed" if vr["exitCode"] == 0 else "failed")
                    except Exception as exc:  # noqa: BLE001 - report, never crash the tick
                        record["verificationStatus"] = "error"
                        record["error"] = f"Verification could not run: {exc}"
                    if record["verificationStatus"] != "passed":
                        record["status"] = "verification_failed"
            return record
        except Exception as exc:  # noqa: BLE001 - every failure is recorded per experiment
            record["status"] = "error"
            record["error"] = str(exc)
            return record
        finally:
            record["finishedAt"] = _now()
            states.mkdir(parents=True, exist_ok=True)
            save_json(state_file, record)
            short = record["commitSha"][:8] if record["commitSha"] else "-"
            line = f"{exp_id}: {record['status']} ({short})"
            if record["error"]:
                line += f" - {record['error']}"
            _say(line)
    finally:
        lock.release()


def check(config_path: Path, only: str = "") -> int:
    config = load_config(config_path)
    for job in config["experiments"]:
        if only and job["id"] != only:
            continue
        print(f"CHECK {job['id']}: {job['repo']} [{job['branch']}] -> {job['entrypoint']} ({job['runMode']})")
    return 0


def poll(config_path: Path, only: str = "", force: bool = False) -> int:
    config_path = config_path.resolve()
    config = load_config(config_path)
    root = config_path.parent
    if (root / "paused.flag").exists():
        print("Poller paused. No updates or commands were run.")
        return 0
    jobs = [j for j in config["experiments"] if not only or j["id"] == only]
    if only and not jobs:
        raise PollerError(f"Unknown experiment id '{only}'")
    for sub in ("workspaces", "state", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    if not jobs:
        print("No experiments configured.")
        return 0
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda j: poll_experiment(j, root, config["workerId"], force), jobs))
    return 1 if any(r["status"] in FAILED_STATES for r in results) else 0
