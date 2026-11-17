import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from git_repo_poller import launcher
from git_repo_poller.common import FileLock, load_config

from .helpers import REPO_ROOT, RUN_PY, GitTestCase


class InstallTests(GitTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "install root"
        files = {"poller.ps1": (REPO_ROOT / "poller.ps1").read_text()}
        for f in (REPO_ROOT / "git_repo_poller").glob("*.py"):
            files[f"git_repo_poller/{f.name}"] = f.read_text()
        self.engine_repo, self.engine_seed = self.make_remote("engine", files)
        self.exp_repo, self.exp_seed = self.make_remote("exp", {"run.py": RUN_PY})
        self.source_cfg = self.write_config(self.tmp / "src", [{
            "id": "exp", "repo": self.exp_repo, "branch": "main", "entrypoint": "run.py"}],
            autoUpdate=True, engineRemote=self.engine_repo, engineBranch="main")

    def setup_cli(self, *extra):
        return self.cli("setup", "--install-root", str(self.root), *extra)

    def install(self):
        r = self.setup_cli("--non-interactive", "--config", str(self.source_cfg))
        self.assertEqual(r.returncode, 0, r.stdout)

    def launch(self, *extra):
        # Run from the INSTALLED bootstrap copy, like watch does.
        return self.cli("launch", "--install-root", str(self.root), *extra, pythonpath=self.root, cwd=self.root)

    def current(self):
        return (self.root / "current-version.txt").read_text().strip()

    def test_dry_run_writes_nothing(self):
        r = self.setup_cli("--non-interactive", "--config", str(self.source_cfg), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("DRY RUN", r.stdout)
        self.assertFalse(self.root.exists())

    def test_non_interactive_needs_config(self):
        self.assertEqual(self.setup_cli("--non-interactive").returncode, 1)

    def test_interactive_wizard(self):
        # URL, branch, long-running?, program, interval (bad value is asked again).
        answers = "\n".join([self.exp_repo, "main", "n", "run.py", "2", "7"])
        r = self.cli("setup", "--install-root", str(self.root), stdin=answers + "\n")
        self.assertEqual(r.returncode, 0, r.stdout)
        cfg = json.loads((self.root / "config.json").read_text())
        self.assertEqual((cfg["intervalSeconds"], cfg["autoUpdate"]), (7, False))
        self.assertEqual((cfg["experiment"]["id"], cfg["experiment"]["runMode"]), ("exp", "on_change"))
        self.assertIn("Change settings", r.stdout)

    def test_no_arguments_sets_up_then_starts_and_status_reports(self):
        # No command = start; with LOCALAPPDATA redirected, the default install root is temporary.
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), LOCALAPPDATA=str(self.tmp / "appdata"),
                   GIT_POLLER_ENGINE_REMOTE=self.engine_repo)
        root = self.tmp / "appdata" / "GitRepoPoller"

        def run(*args, stdin=""):
            return subprocess.run([sys.executable, "-m", "git_repo_poller", *args], cwd=str(REPO_ROOT), env=env,
                                  input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        r = run("status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not set up", r.stdout)
        # Long-running program answer "y", then decline "Start now?".
        r = run(stdin="\n".join([self.exp_repo, "", "y", "run.py", "", "n"]) + "\n")
        self.assertEqual(r.returncode, 0, r.stdout)
        cfg = json.loads((root / "config.json").read_text())
        self.assertEqual((cfg["experiment"]["runMode"], cfg["intervalSeconds"]), ("service", 30))
        self.assertIn("Has not run yet", run("status").stdout)
        self.assertIn("help", run("--help").stdout)
        self.assertNotIn("sync-results", run("--help").stdout)

    def test_installed_bootstrap_is_initial_fallback_for_incompatible_engine(self):
        # A setup wizard from a newer branch can write a config that the
        # released engine does not understand yet (here: service mode).
        common = self.engine_seed / "git_repo_poller" / "common.py"
        self.commit(self.engine_seed, {"git_repo_poller/common.py": common.read_text().replace(
            'RUN_MODES = ("on_change", "every_tick", "service")',
            'RUN_MODES = ("on_change", "every_tick")')}, "older engine without service mode")
        cfg = json.loads(self.source_cfg.read_text())
        cfg["experiment"]["runMode"] = "service"
        self.source_cfg.write_text(json.dumps(cfg))
        self.install()

        config = load_config(self.root / "config.json")
        selected = launcher.select_engine(self.root.resolve(), config, no_update=True)
        self.assertEqual(selected, self.root.resolve())
        self.assertEqual(launcher._python_in(selected, ["check", "--config", str(self.root / "config.json")]), 0)

    def test_install_launch_self_update_and_fallback(self):
        self.install()
        for name in ("config.json", "poller.ps1", "git_repo_poller/cli.py", "engine/.git"):
            self.assertTrue((self.root / name).exists(), name)
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stdout)
        first = self.current()
        self.assertEqual(self.state(self.root, "exp")["status"], "succeeded")

        # A new engine commit is picked up between ticks.
        new = self.commit(self.engine_seed, {"NOTES.md": "update"}, "engine update")
        self.launch()
        self.assertEqual(self.current(), new)
        self.assertNotEqual(first, new)

        # A broken engine fails its check; last known good stays selected and ticks keep working.
        self.commit(self.engine_seed, {"git_repo_poller/cli.py": "this is not python("}, "broken")
        r = self.launch()
        self.assertEqual(self.current(), new, r.stdout)
        self.assertIn("failed its check", r.stdout)
        self.assertEqual(self.state(self.root, "exp")["status"], "unchanged")

        # --no-update keeps the selected snapshot as is.
        self.assertEqual(self.launch("--no-update").returncode, 0)
        self.assertEqual(self.current(), new)

    def test_auto_update_off_by_default_and_dirty_engine_refused(self):
        cfg = json.loads(self.source_cfg.read_text())
        del cfg["autoUpdate"]
        self.source_cfg.write_text(json.dumps(cfg))
        self.install()
        self.launch()
        first = self.current()
        self.commit(self.engine_seed, {"NOTES.md": "update"}, "engine update")
        self.launch()
        self.assertEqual(self.current(), first)

        cfg["autoUpdate"] = True
        (self.root / "config.json").write_text(json.dumps(cfg))
        (self.root / "engine" / "local.txt").write_text("dirty")
        r = self.launch()
        self.assertEqual(r.returncode, 1)
        self.assertIn("dirty", r.stdout)

    def test_watch_keeps_polling_until_stopped(self):
        cfg = json.loads(self.source_cfg.read_text())
        cfg["intervalSeconds"] = 5
        self.source_cfg.write_text(json.dumps(cfg))
        self.install()
        env = dict(os.environ, PYTHONPATH=str(self.root))
        proc = subprocess.Popen([sys.executable, "-m", "git_repo_poller", "watch", "--install-root",
                                 str(self.root)], cwd=str(self.root), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_for(lambda: self.run_count(self.root, "exp") == 1)
            self.commit(self.exp_seed, {"code.txt": "v2"}, "v2")
            self.wait_for(lambda: self.run_count(self.root, "exp") == 2)
        finally:
            proc.kill()
            proc.wait()

    def wait_for(self, condition, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.3)
        self.fail("condition not reached in time")

    def test_replace_experiment_preserves_previous_workspace_and_results(self):
        self.install()
        self.launch()
        old_workspace = self.root / "workspaces" / "exp"
        self.assertTrue(old_workspace.exists())
        old_runs = self.run_count(self.root, "exp")
        new_repo, _ = self.make_remote("replacement", {"run.py": RUN_PY})
        # Plain "setup" on an existing installation asks the questions again.
        answers = "\n".join([new_repo, "main", "n", "run.py", ""])
        r = self.cli("setup", "--install-root", str(self.root), stdin=answers + "\n")
        self.assertEqual(r.returncode, 0, r.stdout)
        cfg = json.loads((self.root / "config.json").read_text())
        self.assertEqual(cfg["experiment"]["id"], "replacement")
        self.assertTrue(old_workspace.exists())
        self.assertEqual(self.run_count(self.root, "exp"), old_runs)
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual(self.run_count(self.root, "replacement"), 1)

        # A different repo with the same name gets a new ID, so old files are never reused.
        twin_repo, _ = self.make_remote("twin/replacement", {"run.py": RUN_PY})
        answers = "\n".join([twin_repo, "main", "n", "run.py", ""])
        r = self.cli("setup", "--install-root", str(self.root), "--replace-experiment", stdin=answers + "\n")
        self.assertEqual(r.returncode, 0, r.stdout)
        cfg = json.loads((self.root / "config.json").read_text())
        self.assertEqual(cfg["experiment"]["id"], "replacement-2")

    def test_replace_experiment_is_refused_while_worker_is_busy(self):
        self.install()
        config_before = (self.root / "config.json").read_text()
        lock = FileLock(self.root / "state" / "poller.lock")
        self.assertTrue(lock.acquire())
        try:
            r = self.setup_cli("--replace-experiment")
        finally:
            lock.release()
        self.assertEqual(r.returncode, 1)
        self.assertIn("currently running", r.stdout)
        self.assertEqual((self.root / "config.json").read_text(), config_before)

    def test_legacy_interval_minutes(self):
        cfg = json.loads(self.source_cfg.read_text())
        cfg["intervalMinutes"] = 2
        self.source_cfg.write_text(json.dumps(cfg))
        self.install()
        self.assertEqual(json.loads((self.root / "config.json").read_text())["intervalSeconds"], 120)

    def test_pause_resume_repair_uninstall(self):
        self.install()
        self.setup_cli("--pause")
        self.assertIn("paused", self.launch().stdout)
        self.assertFalse((self.root / "workspaces" / "exp").exists())
        self.setup_cli("--resume")
        self.launch()
        shutil.rmtree(self.root / "git_repo_poller")
        r = self.setup_cli("--repair")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((self.root / "git_repo_poller" / "cli.py").exists())
        r = self.setup_cli("--uninstall")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((self.root / "config.json").exists())
        self.assertTrue((self.root / "workspaces" / "exp").exists())
        self.assertTrue(any((self.root / "runs" / "exp").iterdir()))


@unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
class WrapperTests(GitTestCase):
    def pwsh(self, wrapper: Path, *args):
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(wrapper), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_wrapper_forwards_arguments_and_exit_code(self):
        r = self.pwsh(REPO_ROOT / "poller.ps1", "check", "--config",
                      str(REPO_ROOT / "examples" / "config.example.json"))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CHECK local-benchmark", r.stdout)
        r = self.pwsh(REPO_ROOT / "poller.ps1", "check", "--config", str(self.tmp / "missing.json"))
        self.assertEqual(r.returncode, 1)

    def test_installed_wrapper_runs_a_tick(self):
        repo, _ = self.make_remote("exp", {"run.py": RUN_PY})
        engine, _ = self.make_remote("engine", {"README.md": "engine"})
        engine_seed = self.tmp / "engine seed"
        shutil.copytree(REPO_ROOT / "git_repo_poller", engine_seed / "git_repo_poller",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.commit(engine_seed, {}, "package")
        cfg = self.write_config(self.tmp / "src", [{"id": "exp", "repo": repo, "branch": "main",
                                                    "entrypoint": "run.py"}], engineRemote=engine)
        root = self.tmp / "inst"
        r = self.pwsh(REPO_ROOT / "poller.ps1", "setup", "--install-root", str(root), "--non-interactive",
                      "--config", str(cfg))
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.pwsh(root / "poller.ps1", "launch", "--install-root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.state(root, "exp")["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
