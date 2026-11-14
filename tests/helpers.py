"""Temporary local Git remotes and dummy entrypoints. No network, no GPU."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.test"]

# Dummy experiment: records each run inside its own GIT_POLLER_RUN_DIR.
RUN_PY = """import os, pathlib
run_dir = pathlib.Path(os.environ["GIT_POLLER_RUN_DIR"])
(run_dir / "marker.txt").write_text(os.environ["GIT_POLLER_COMMIT"])
print("dummy experiment completed")
"""


def g(where: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(where), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stdout}")
    return proc.stdout.strip()


class GitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="git poller test "))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_remote(self, name: str, files: dict) -> tuple:
        bare = self.tmp / f"{name}.git"
        seed = self.tmp / f"{name} seed"
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True,
                       stderr=subprocess.DEVNULL)
        g(seed, "checkout", "-q", "-b", "main")
        self.commit(seed, files, "initial")
        return str(bare), seed

    def commit(self, seed: Path, files: dict, message: str) -> str:
        for rel, content in files.items():
            path = seed / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        g(seed, "add", "-A")
        g(seed, *IDENTITY, "commit", "-q", "-m", message)
        g(seed, "push", "-q", "origin", "main")
        return g(seed, "rev-parse", "HEAD")

    def write_config(self, root: Path, experiments: list, **extra) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        cfg = {"workerId": "test", "autoUpdate": False, "experiments": experiments, **extra}
        path = root / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def cli(self, *args: str, cwd: Path = REPO_ROOT, pythonpath: Path = REPO_ROOT,
            stdin: str = "") -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=str(pythonpath))
        return subprocess.run([sys.executable, "-m", "git_repo_poller", *args], cwd=str(cwd), env=env,
                              input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def state(self, root: Path, exp_id: str) -> dict:
        return json.loads((root / "state" / f"{exp_id}.json").read_text(encoding="utf-8"))

    def run_count(self, root: Path, exp_id: str) -> int:
        runs = root / "runs" / exp_id
        return len(list(runs.glob("*/marker.txt"))) if runs.exists() else 0
