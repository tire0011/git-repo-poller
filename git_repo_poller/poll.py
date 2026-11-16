"""One poll tick: update the approved checkout and run the experiment on change.

runMode "service" is the exception: the entrypoint is a long-running process
(e.g. an agent). The tick then stays alive as its supervisor, fetching every
intervalSeconds and restarting the process when a relevant commit arrives.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import sync_results
from .common import FileLock, PollerError, git, git_ok, is_under, load_config, load_json, matches_prefix, save_json

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


def _fetch(job: Dict[str, Any], checkout: Path) -> str:
    """Sync results, refuse dirty trees and fetch. Returns the remote branch SHA."""
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
    return git(checkout, "rev-parse", f"origin/{job['branch']}")


def _fast_forward(job: Dict[str, Any], checkout: Path) -> str:
    try:
        git(checkout, "merge", "--ff-only", f"origin/{job['branch']}")
    except PollerError as exc:
        raise PollerError(f"Checkout cannot fast-forward (diverged?); left untouched. {exc}") from None
    return git(checkout, "rev-parse", "HEAD")


def _update_checkout(job: Dict[str, Any], checkout: Path) -> str:
    _fetch(job, checkout)
    return _fast_forward(job, checkout)


def _relevant_change(job: Dict[str, Any], checkout: Path, previous: str, sha: str) -> bool:
    changed = [p for p in git(checkout, "diff", "--name-only", previous, sha, "--").splitlines() if p]
    ignored = job["ignorePaths"] + job["resultPaths"]
    return any(not matches_prefix(p, ignored) for p in changed)


def _new_run_dir(root: Path, exp_id: str, sha: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = root / "runs" / exp_id / f"{stamp}-{sha[:8]}"
    n = 1
    while run_dir.exists():  # never share a run dir between two runs
        n += 1
        run_dir = root / "runs" / exp_id / f"{stamp}-{sha[:8]}-{n}"
    run_dir.mkdir(parents=True)
    return run_dir


def _run_env(run_dir: Path, exp_id: str, sha: str, worker_id: str) -> Dict[str, str]:
    return dict(os.environ, GIT_POLLER_RUN_DIR=str(run_dir), GIT_POLLER_EXPERIMENT_ID=exp_id,
                GIT_POLLER_COMMIT=sha, GIT_POLLER_WORKER_ID=worker_id)


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
            run_dir = _new_run_dir(root, exp_id, sha)
            env = _run_env(run_dir, exp_id, sha, worker_id)
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


class _Job:
    """Windows job object: kills the whole service tree on demand, and also
    when the supervisor dies, so no orphaned agent keeps running unsupervised."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD)
        self._k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self._k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self._k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        proc = self._k32.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        try:
            if not proc or not self._k32.AssignProcessToJobObject(self.handle, proc):
                raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            if proc:
                self._k32.CloseHandle(proc)

    def close(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)
            self._k32.CloseHandle(self.handle)
            self.handle = None


class _Service:
    """A long-running entrypoint in its own process group."""

    def __init__(self, script: Path, cwd: Path, run_dir: Path, env: Dict[str, str]):
        self.stdout_log = run_dir / "run.out.log"
        self.stderr_log = run_dir / "run.err.log"
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._job: Optional[_Job] = None
        with open(self.stdout_log, "wb") as out, open(self.stderr_log, "wb") as err:
            self.proc = subprocess.Popen(entry_command(script), cwd=str(cwd), stdout=out, stderr=err,
                                         stdin=subprocess.DEVNULL, env=env, **kwargs)
        if os.name == "nt":
            try:
                self._job = _Job()
                self._job.assign(self.proc.pid)
            except OSError as exc:
                _say(f"WARNING: no job object for the service ({exc}); it may outlive a killed poller")
                if self._job:
                    self._job.close()
                self._job = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def exit_code(self) -> Optional[int]:
        return self.proc.poll()

    def stop(self, grace: int) -> int:
        """Ask for a graceful stop (Ctrl+Break / SIGTERM), then kill the whole tree."""
        if self.proc.poll() is None:
            try:
                if os.name == "nt":
                    os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=grace)
            except (OSError, subprocess.TimeoutExpired):
                pass
        # Also removes grandchildren left behind after the main process exited.
        if self._job:
            self._job.close()
            self._job = None
        elif self.proc.poll() is None:
            _kill_tree(self.proc)
        elif os.name != "nt":
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return self.proc.returncode if self.proc.returncode is not None else -1


def serve_experiment(job: Dict[str, Any], root: Path, worker_id: str, interval: int) -> Dict[str, Any]:
    """Supervise a long-running entrypoint until paused, Ctrl+C or a setup error.

    Starts it at the current commit, then fetches every interval. A relevant
    commit stops the process, fast-forwards and starts it again; ignored-path
    commits are fast-forwarded without a restart. A process that exits on its
    own is NOT restarted until the next relevant commit (no crash loops).
    Offline, dirty or diverged checkouts keep the current process running.
    """
    exp_id = job["id"]
    states = root / "state"
    state_file = states / f"{exp_id}.json"
    paused = root / "paused.flag"
    checkout = root / "workspaces" / exp_id
    lock = FileLock(states / f"{exp_id}.lock")
    if not lock.acquire():
        _say(f"{exp_id}: busy (already supervised); skipped")
        return {"experimentId": exp_id, "status": "busy"}
    record: Dict[str, Any] = {
        "experimentId": exp_id, "workerId": worker_id, "runMode": "service", "lastSeenSha": "",
        "commitSha": "", "status": "checking", "startedAt": None, "finishedAt": None, "exitCode": None,
        "pid": None, "restarts": 0, "runDir": None, "stdoutLog": None, "stderrLog": None, "error": None,
    }
    service: Optional[_Service] = None

    def save() -> None:
        states.mkdir(parents=True, exist_ok=True)
        save_json(state_file, record)

    def start(sha: str) -> None:
        nonlocal service
        script = resolve_entry(checkout, job["entrypoint"])
        run_dir = _new_run_dir(root, exp_id, sha)
        service = _Service(script, checkout, run_dir, _run_env(run_dir, exp_id, sha, worker_id))
        record.update(commitSha=sha, lastSeenSha=sha, status="running", startedAt=_now(), finishedAt=None,
                      exitCode=None, pid=service.pid, runDir=str(run_dir), stdoutLog=str(service.stdout_log),
                      stderrLog=str(service.stderr_log))
        save()
        _say(f"{exp_id}: service started ({sha[:8]}, pid {service.pid})")

    def stop(status: str) -> None:
        nonlocal service
        if service:
            record.update(exitCode=service.stop(job["stopTimeoutSeconds"]), finishedAt=_now(), pid=None)
            service = None
        record["status"] = status
        save()

    try:
        try:
            _ensure_checkout(job, checkout)
            start(_update_checkout(job, checkout))
        except Exception as exc:  # noqa: BLE001 - recorded; watch retries next tick
            record.update(status="error", error=str(exc), finishedAt=_now())
            save()
            _say(f"{exp_id}: error - {exc}")
            return record
        while True:
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not paused.exists():
                if service and service.exit_code() is not None:
                    break
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            if paused.exists():
                stop("stopped")
                _say(f"{exp_id}: paused; service stopped")
                return record
            if service and service.exit_code() is not None:
                code = service.stop(job["stopTimeoutSeconds"])  # reaps leftover children
                service = None
                record.update(status="exited" if code == 0 else "failed", exitCode=code,
                              finishedAt=_now(), pid=None)
                save()
                _say(f"{exp_id}: service exited with code {code}; waiting for a new commit")
            try:
                target = _fetch(job, checkout)
                head = git(checkout, "rev-parse", "HEAD")
                if target == head:
                    if record["error"]:
                        record["error"] = None
                        save()
                    continue
                if not git_ok(checkout, "merge-base", "--is-ancestor", head, target):
                    raise PollerError("Checkout cannot fast-forward (diverged?); left untouched")
                if not _relevant_change(job, checkout, head, target):
                    record.update(lastSeenSha=_fast_forward(job, checkout), error=None)
                    save()
                    _say(f"{exp_id}: ignored_change ({target[:8]}); service not restarted")
                    continue
            except PollerError as exc:
                # Offline, dirty or diverged: keep the current process running.
                if record["error"] != str(exc):
                    _say(f"{exp_id}: update skipped - {exc}")
                record["error"] = str(exc)
                save()
                continue
            was_running = service is not None
            stop("restarting")
            sha = _fast_forward(job, checkout)
            record["error"] = None
            if was_running:
                record["restarts"] += 1
            start(sha)
    except KeyboardInterrupt:
        stop("stopped")
        _say(f"{exp_id}: interrupted; service stopped")
        return record
    except Exception as exc:  # noqa: BLE001 - never leave a service running unsupervised
        record["error"] = str(exc)
        stop("error")
        _say(f"{exp_id}: error - {exc}")
        return record
    finally:
        if service:
            service.stop(job["stopTimeoutSeconds"])
        lock.release()


def check(config_path: Path) -> int:
    config = load_config(config_path)
    job = config["experiment"]
    print(f"CHECK {job['id']}: {job['repo']} [{job['branch']}] -> {job['entrypoint']} ({job['runMode']})")
    return 0


def poll(config_path: Path, force: bool = False) -> int:
    config_path = config_path.resolve()
    root = config_path.parent
    if (root / "paused.flag").exists():
        print("Poller paused. No updates or commands were run.")
        return 0

    # One lock for the entire installation. The setup wizard takes the same
    # lock before changing experiments, preventing switches during a run.
    lock = FileLock(root / "state" / "poller.lock")
    if not lock.acquire():
        print("Poller busy: an experiment or configuration change is in progress.")
        return 0
    try:
        config = load_config(config_path)
        for sub in ("workspaces", "state", "runs"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        job = config["experiment"]
        if job["runMode"] == "service":
            result = serve_experiment(job, root, config["workerId"], config["intervalSeconds"])
        else:
            result = poll_experiment(job, root, config["workerId"], force)
        return 1 if result["status"] in FAILED_STATES else 0
    finally:
        lock.release()
