import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from .helpers import REPO_ROOT, GitTestCase

# Dummy long-running agent: records its commit and pid, then runs until stopped.
SERVICE_PY = """import os, pathlib, time
run_dir = pathlib.Path(os.environ["GIT_POLLER_RUN_DIR"])
(run_dir / "pid.txt").write_text(str(os.getpid()))
(run_dir / "marker.txt").write_text(os.environ["GIT_POLLER_COMMIT"])
while True:
    time.sleep(0.2)
"""


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], stdout=subprocess.PIPE,
                             text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def profile(repo, **kw):
    base = {"id": "agent", "repo": repo, "branch": "main", "entrypoint": "run.py", "runMode": "service",
            "stopTimeoutSeconds": 5, "ignorePaths": ["docs/"]}
    base.update(kw)
    return base


class ServiceTests(GitTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "install"
        self.supervisor = None

    def start(self, repo, **kw):
        cfg = self.write_config(self.root, [profile(repo, **kw)], intervalSeconds=5)
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        self.log = open(self.tmp / "supervisor.log", "w")
        self.addCleanup(self.log.close)
        self.supervisor = subprocess.Popen([sys.executable, "-m", "git_repo_poller", "poll", "--config", str(cfg)],
                                           cwd=str(REPO_ROOT), env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(self._kill_supervisor)

    def _kill_supervisor(self):
        if self.supervisor and self.supervisor.poll() is None:
            self.supervisor.kill()
            self.supervisor.wait()

    def pause_and_wait(self) -> int:
        (self.root / "paused.flag").write_text("paused")
        return self.supervisor.wait(timeout=30)

    def state(self, *_):
        try:
            return json.loads((self.root / "state" / "agent.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def pids(self):
        return [int(p.read_text()) for p in sorted((self.root / "runs" / "agent").glob("*/pid.txt"))
                if p.read_text()]

    def wait_for(self, condition, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.3)
        self.fail("condition not reached in time; supervisor log:\n" +
                  (self.tmp / "supervisor.log").read_text(errors="replace"))

    def test_relevant_commit_restarts_service_ignored_commit_does_not(self):
        repo, seed = self.make_remote("agent", {"run.py": SERVICE_PY})
        self.start(repo)
        self.wait_for(lambda: len(self.pids()) == 1)
        first = self.pids()[0]
        self.assertTrue(alive(first))
        self.assertEqual(self.state()["status"], "running")

        docs = self.commit(seed, {"docs/readme.md": "only docs"}, "docs")
        self.wait_for(lambda: self.state().get("lastSeenSha") == docs)
        self.assertEqual(len(self.pids()), 1)
        self.assertTrue(alive(first))

        code = self.commit(seed, {"code.txt": "v2"}, "code change")
        self.wait_for(lambda: len(self.pids()) == 2 and self.state().get("status") == "running")
        st = self.state()
        self.assertEqual((st["commitSha"], st["restarts"]), (code, 1))
        self.wait_for(lambda: not alive(first))
        second = self.pids()[1]
        self.assertTrue(alive(second))

        self.assertEqual(self.pause_and_wait(), 0)
        self.assertEqual(self.state()["status"], "stopped")
        self.wait_for(lambda: not alive(second))

    def test_exited_service_is_not_restarted_until_new_commit(self):
        crash = "import os, pathlib, sys\n" \
                "(pathlib.Path(os.environ['GIT_POLLER_RUN_DIR']) / 'marker.txt').write_text('x')\nsys.exit(3)\n"
        repo, seed = self.make_remote("agent", {"run.py": crash})
        self.start(repo)
        self.wait_for(lambda: self.state().get("status") == "failed")
        self.assertEqual(self.state()["exitCode"], 3)
        time.sleep(6)  # at least one more interval
        self.assertEqual(self.run_count(self.root, "agent"), 1)
        self.commit(seed, {"run.py": SERVICE_PY}, "fixed agent")
        self.wait_for(lambda: self.state().get("status") == "running" and len(self.pids()) == 1)
        self.assertEqual(self.state()["restarts"], 0)
        self.assertEqual(self.pause_and_wait(), 0)

    def test_offline_or_dirty_checkout_keeps_service_running(self):
        repo, seed = self.make_remote("agent", {"run.py": SERVICE_PY})
        self.start(repo)
        self.wait_for(lambda: len(self.pids()) == 1)
        pid = self.pids()[0]
        self.commit(seed, {"code.txt": "v2"}, "v2")
        hidden = Path(repo + ".offline")
        shutil.move(repo, hidden)
        self.wait_for(lambda: self.state().get("error"))
        self.assertEqual(self.state()["status"], "running")
        self.assertTrue(alive(pid))

        checkout = self.root / "workspaces" / "agent"
        (checkout / "local.txt").write_text("keep me")
        shutil.move(str(hidden), repo)
        self.wait_for(lambda: "Dirty" in (self.state().get("error") or ""))
        self.assertTrue(alive(pid))
        self.assertEqual(len(self.pids()), 1)
        self.assertTrue((checkout / "local.txt").exists())

        (checkout / "local.txt").unlink()
        self.wait_for(lambda: len(self.pids()) == 2 and self.state().get("status") == "running")
        self.assertIsNone(self.state()["error"])
        self.assertEqual(self.pause_and_wait(), 0)

    @unittest.skipUnless(os.name == "nt", "job objects are Windows-only")
    def test_killed_supervisor_takes_service_down(self):
        repo, _ = self.make_remote("agent", {"run.py": SERVICE_PY})
        self.start(repo)
        self.wait_for(lambda: len(self.pids()) == 1)
        pid = self.pids()[0]
        self.supervisor.kill()
        self.supervisor.wait()
        self.wait_for(lambda: not alive(pid), timeout=15)

    def test_service_mode_rejects_verify_entry(self):
        repo, _ = self.make_remote("agent", {"run.py": SERVICE_PY})
        cfg = self.write_config(self.root, [profile(repo, verifyEntry="verify.py")])
        r = self.cli("check", "--config", str(cfg))
        self.assertEqual(r.returncode, 1)
        self.assertIn("verifyEntry", r.stdout)
        cfg = self.write_config(self.root, [profile(repo)])
        self.assertIn("(service)", self.cli("check", "--config", str(cfg)).stdout)


if __name__ == "__main__":
    unittest.main()
