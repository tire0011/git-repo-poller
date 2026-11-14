"""Opt-in publishing of ALLOWLISTED result paths (port of the benchmark's sync-results.ps1).

Commits pending changes under the result paths, rebases result-only local commits
onto the remote branch and pushes them. It never resets, force-pushes or touches
commits that change anything outside the result paths.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, List

from .common import PollerError, git, matches_prefix


def _normalize(result_paths: List[str]) -> List[str]:
    prefixes = []
    for raw in result_paths:
        p = str(raw).replace("\\", "/").strip()
        if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
            raise PollerError(f"Absolute result path not allowed: {raw}")
        p = p.strip("/")
        parts = p.split("/")
        if not p or p == "." or ".." in parts or "*" in p:
            raise PollerError(f"Unsafe result path: {raw}")
        prefixes.append(p + "/")
    if not prefixes:
        raise PollerError("Explicit result paths required")
    return prefixes


def _lines(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.strip()]


def sync(repo: Path, branch: str, expected_remote: str, result_paths: List[str],
         commit_pending: bool = True, log: Callable[[str], None] = print, attempts: int = 4) -> None:
    prefixes = _normalize(result_paths)
    if not (repo / ".git").exists():
        raise PollerError(f"Not a Git checkout: {repo}")
    remote = git(repo, "remote", "get-url", "origin")
    current = git(repo, "branch", "--show-current")
    if remote != expected_remote or current != branch:
        raise PollerError(f"Refusing results sync: unexpected remote/branch {remote} [{current}]")

    # -z keeps file names with spaces/quotes intact.
    tracked = [p for p in git(repo, "diff", "--name-only", "-z", "HEAD").split("\0") if p]
    outside = [p for p in tracked if not matches_prefix(p, prefixes)]
    if outside:
        raise PollerError(f"Non-result changes: {', '.join(outside)}. Preserved.")
    staged = [p for p in git(repo, "diff", "--cached", "--name-only", "-z").split("\0") if p]
    if any(not matches_prefix(p, prefixes) for p in staged):
        raise PollerError("Non-result files are staged. Refusing automatic commit/rebase.")

    if commit_pending and _lines(git(repo, "status", "--porcelain", "--", *prefixes)):
        stage = [p for p in prefixes if (repo / p).exists() or _lines(git(repo, "ls-files", "--", p))]
        if stage:
            git(repo, "add", "-A", "--", *stage)
            git(repo, "commit", "-m", "Experiment results " + time.strftime("%Y-%m-%d %H:%M:%S"))
            log("committed changes under approved result paths")

    if _lines(git(repo, "status", "--porcelain", "--untracked-files=no")):
        raise PollerError("Tracked worktree changes remain. Refusing automatic rebase.")

    for attempt in range(1, attempts + 1):
        git(repo, "fetch", "origin", branch, "--quiet")
        local = git(repo, "rev-parse", "HEAD")
        upstream = git(repo, "rev-parse", f"origin/{branch}")
        if local != upstream:
            commits = _lines(git(repo, "rev-list", "--reverse", f"origin/{branch}..HEAD"))
            for sha in commits:
                parents = git(repo, "rev-list", "--parents", "-n", "1", sha).split()
                if len(parents) != 2:
                    raise PollerError(f"Local merge/root commit {sha} needs manual review")
                paths = [p for p in git(repo, "diff-tree", "--no-commit-id", "--name-only", "-z", "-r", sha)
                         .split("\0") if p]
                if not paths or any(not matches_prefix(p, prefixes) for p in paths):
                    raise PollerError(f"Local commit {sha} contains non-result files; no automatic rebase.")
            if commits:
                log(f"rebasing {len(commits)} result-only commit(s) on origin/{branch}")
                if subprocess.run(["git", "-C", str(repo), "rebase", f"origin/{branch}"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                    subprocess.run(["git", "-C", str(repo), "rebase", "--abort"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    raise PollerError("Rebase conflicted; aborted without discarding local commits.")
            else:
                git(repo, "merge", "--ff-only", f"origin/{branch}")
        if not _lines(git(repo, "rev-list", f"origin/{branch}..HEAD")):
            return
        log(f"publishing result-only commits (attempt {attempt}/{attempts})")
        if subprocess.run(["git", "-C", str(repo), "push", "origin", branch],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            log("experiment results published")
            return
        time.sleep(2)
    raise PollerError(f"Result-only push failed after {attempts} attempts; local commits preserved.")
