"""Regression tests for package-based installations (release ZIP / pip install).

The poller is run from an unzipped release ZIP or from a clean virtual
environment, without any checkout of the poller repository and without
cloning the engine (autoUpdate disabled). Only local temporary Git remotes
are used; the configured engine remote is unreachable on purpose.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
import zipfile
from pathlib import Path

from git_repo_poller import __version__

from .helpers import REPO_ROOT, RUN_PY, GitTestCase
from .test_service import SERVICE_PY, alive

SOURCE_ROOT = Path(os.environ.get("GIT_POLLER_SOURCE_ROOT") or REPO_ROOT).resolve()
sys.path.insert(0, str(SOURCE_ROOT / "tools"))
import make_zip  # noqa: E402

# Nothing listens here: any engine clone attempt fails immediately.
UNREACHABLE_ENGINE = "https://127.0.0.1:1/git-repo-poller-engine.git"


def run_pkg(pkg_dir, *args, cwd, stdin=""):
    """Run the CLI from an extracted ZIP (pkg_dir) or from the installed
    distribution (pkg_dir=None: no PYTHONPATH, the interpreter's own package)."""
    env = dict(os.environ)
    env.pop("GIT_POLLER_ENGINE_REMOTE", None)
    if pkg_dir is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(pkg_dir)
    return subprocess.run([sys.executable, "-m", "git_repo_poller", *args], cwd=str(cwd), env=env,
                          input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(condition, timeout=40, fail=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.3)
    raise AssertionError(fail or "condition not reached in time")


def state_of(root: Path, exp_id: str) -> dict:
    try:
        return json.loads((root / "state" / f"{exp_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class PackageTestCase(GitTestCase):
    def write_pkg_config(self, experiment, **extra):
        cfg = {"workerId": "test", "autoUpdate": False, "engineRemote": UNREACHABLE_ENGINE,
               "engineBranch": "main", "experiment": experiment}
        cfg.update(extra)
        path = self.tmp / "cfg" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path


class ZipBuildTests(PackageTestCase):
    def test_zip_contains_package_metadata_and_wrapper(self):
        out = make_zip.build(self.tmp / "dist")
        self.assertEqual(out.name, f"git-repo-poller-{__version__}.zip")
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            pyproject = zf.read("pyproject.toml").decode("utf-8")
        for expected in ("pyproject.toml", "README.md", "LICENSE", "poller.ps1",
                         "git_repo_poller/__init__.py", "git_repo_poller/cli.py",
                         "git_repo_poller/common.py", "git_repo_poller/installer.py",
                         "git_repo_poller/launcher.py", "git_repo_poller/poll.py",
                         "git_repo_poller/sync_results.py", "examples/config.example.json"):
            self.assertIn(expected, names)
        self.assertFalse(any("__pycache__" in n for n in names))
        self.assertIn('git-repo-poller = "git_repo_poller.cli:main"', pyproject)
        self.assertIn('attr = "git_repo_poller.__version__"', pyproject)


class ZipInstallTests(PackageTestCase):
    """Unzipped release ZIP, used without the poller source repo and without an engine clone."""

    def setUp(self):
        super().setUp()
        self.pkg = self.tmp / "pkg"
        with zipfile.ZipFile(make_zip.build(self.tmp / "dist")) as zf:
            zf.extractall(self.pkg)
        self.root = self.tmp / "install root"
        self.exp_repo, self.exp_seed = self.make_remote("exp", {"run.py": RUN_PY})

    def test_cli_commands_work_from_unzipped_zip(self):
        r = run_pkg(self.pkg, "--version", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(__version__, r.stdout)
        cfg = self.write_pkg_config({"id": "exp", "repo": self.exp_repo, "branch": "main",
                                     "entrypoint": "run.py"})
        r = run_pkg(self.pkg, "check", "--config", str(cfg), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CHECK exp", r.stdout)
        self.assertIn("git-repo-poller", run_pkg(self.pkg, "--help", cwd=self.tmp).stdout)

    def test_setup_launch_status_without_engine_clone_or_source_repo(self):
        cfg = self.write_pkg_config({"id": "exp", "repo": self.exp_repo, "branch": "main",
                                     "entrypoint": "run.py"})
        r = run_pkg(self.pkg, "setup", "--install-root", str(self.root), "--non-interactive",
                    "--config", str(cfg), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no engine clone", r.stdout)
        self.assertFalse((self.root / "engine").exists())
        self.assertTrue((self.root / "config.json").exists())
        self.assertTrue((self.root / "git_repo_poller" / "cli.py").exists())
        self.assertTrue((self.root / "poller.ps1").exists())

        r = run_pkg(self.pkg, "launch", "--install-root", str(self.root), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("installed bootstrap", r.stdout)
        self.assertEqual(self.state(self.root, "exp")["status"], "succeeded")
        self.assertEqual(self.run_count(self.root, "exp"), 1)

        r = run_pkg(self.pkg, "status", "--install-root", str(self.root), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("succeeded", r.stdout)

        # A second tick without a new commit does not rerun the experiment.
        r = run_pkg(self.pkg, "launch", "--install-root", str(self.root), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.run_count(self.root, "exp"), 1)

        # A new commit in the watched repo triggers exactly one rerun.
        self.commit(self.exp_seed, {"code.txt": "v2"}, "v2")
        r = run_pkg(self.pkg, "launch", "--install-root", str(self.root), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.run_count(self.root, "exp"), 2)

    def test_auto_update_true_still_clones_the_engine(self):
        cfg = self.write_pkg_config({"id": "exp", "repo": self.exp_repo, "branch": "main",
                                     "entrypoint": "run.py"}, autoUpdate=True)
        r = run_pkg(self.pkg, "setup", "--install-root", str(self.root), "--non-interactive",
                    "--config", str(cfg), cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Engine clone failed", r.stdout)
        self.assertFalse((self.root / "config.json").exists())

    def test_git_clone_install_still_clones_engine_when_auto_update_off(self):
        # Running from the source checkout (it has .git): existing behavior is unchanged.
        engine_repo, _ = self.make_remote("engine", {"README.md": "engine"})
        cfg = self.write_pkg_config({"id": "exp", "repo": self.exp_repo, "branch": "main",
                                     "entrypoint": "run.py"}, engineRemote=engine_repo)
        r = self.cli("setup", "--install-root", str(self.root), "--non-interactive", "--config", str(cfg),
                     pythonpath=SOURCE_ROOT, cwd=SOURCE_ROOT)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((self.root / "engine" / ".git").exists())

    def test_service_mode_supervises_and_restarts_from_package(self):
        repo, seed = self.make_remote("agent", {"run.py": SERVICE_PY})
        cfg = self.write_pkg_config({"id": "agent", "repo": repo, "branch": "main", "entrypoint": "run.py",
                                     "runMode": "service", "stopTimeoutSeconds": 5,
                                     "ignorePaths": ["docs/"]}, intervalSeconds=5)
        r = run_pkg(self.pkg, "setup", "--install-root", str(self.root), "--non-interactive",
                    "--config", str(cfg), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((self.root / "engine").exists())

        env = dict(os.environ, PYTHONPATH=str(self.pkg))
        env.pop("GIT_POLLER_ENGINE_REMOTE", None)
        log = open(self.tmp / "supervisor.log", "w")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-m", "git_repo_poller", "launch", "--install-root",
                                 str(self.root)], cwd=str(self.tmp), env=env, stdout=log,
                                stderr=subprocess.STDOUT)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        try:
            log = lambda: (self.tmp / "supervisor.log").read_text(errors="replace")  # noqa: E731
            wait_for(lambda: state_of(self.root, "agent").get("status") == "running",
                     fail="service did not start; log:\n" + log())
            first_pid = state_of(self.root, "agent")["pid"]
            self.assertTrue(alive(first_pid))

            code = self.commit(seed, {"code.txt": "v2"}, "code change")
            wait_for(lambda: state_of(self.root, "agent").get("restarts") == 1
                     and state_of(self.root, "agent").get("status") == "running",
                     fail="service was not restarted; log:\n" + log())
            st = state_of(self.root, "agent")
            self.assertEqual(st["commitSha"], code)
            self.assertFalse(alive(first_pid))
            self.assertTrue(alive(st["pid"]))

            (self.root / "paused.flag").write_text("paused")
            self.assertEqual(proc.wait(timeout=30), 0)
            self.assertEqual(state_of(self.root, "agent")["status"], "stopped")
            self.assertFalse(alive(st["pid"]))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class PipInstallTests(PackageTestCase):
    """Install the release ZIP into a clean virtual environment with pip."""

    def setUp(self):
        super().setUp()
        self.zip_path = make_zip.build(self.tmp / "dist")
        self.venv = self.tmp / "venv"
        r = subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(self.venv)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            self.skipTest(f"venv creation failed: {r.stdout[-300:]}")
        scripts = self.venv / ("Scripts" if os.name == "nt" else "bin")
        self.py = scripts / ("python.exe" if os.name == "nt" else "python")
        self.console = scripts / ("git-repo-poller.exe" if os.name == "nt" else "git-repo-poller")

    def _pip_install_zip(self):
        env = dict(os.environ, PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                   PIP_RETRIES="0", PIP_TIMEOUT="5")
        base = [str(self.py), "-m", "pip", "install", str(self.zip_path)]
        r = subprocess.run(base, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=300, env=env)
        if r.returncode == 0:
            return
        # Offline fallback: build without isolation using the host's setuptools.
        r = subprocess.run(base + ["--no-build-isolation"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=300, env=env)
        if r.returncode != 0:
            self.skipTest("pip install of the ZIP failed (offline and no setuptools?): " + r.stdout[-400:])

    def test_clean_environment_install_and_run(self):
        self._pip_install_zip()
        self.assertTrue(self.console.exists(), "console script git-repo-poller was not installed")

        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("GIT_POLLER_ENGINE_REMOTE", None)

        def run(*args):
            return subprocess.run([str(self.console), *args], cwd=str(self.tmp), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        r = run("--version")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(__version__, r.stdout)

        repo, _ = self.make_remote("exp", {"run.py": RUN_PY})
        cfg = self.write_pkg_config({"id": "exp", "repo": repo, "branch": "main", "entrypoint": "run.py"})
        r = run("check", "--config", str(cfg))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CHECK exp", r.stdout)

        root = self.tmp / "install"
        r = run("setup", "--install-root", str(root), "--non-interactive", "--config", str(cfg))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((root / "engine").exists())
        self.assertTrue((root / "config.json").exists())
        self.assertTrue((root / "git_repo_poller" / "cli.py").exists())
        # A pip install has no poller.ps1 at hand: the console script is offered instead.
        self.assertFalse((root / "poller.ps1").exists())
        self.assertIn("git-repo-poller start", r.stdout)

        r = run("launch", "--install-root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.state(root, "exp")["status"], "succeeded")
        self.assertEqual(self.run_count(root, "exp"), 1)

        r = run("status", "--install-root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("succeeded", r.stdout)


if __name__ == "__main__":
    unittest.main()
