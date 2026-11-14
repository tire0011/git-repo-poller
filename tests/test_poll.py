import json
import shutil
import threading
import time
import unittest
from pathlib import Path

from git_repo_poller.common import FileLock

from .helpers import IDENTITY, RUN_PY, GitTestCase, g


def profile(exp_id, repo, **kw):
    base = {"id": exp_id, "repo": repo, "branch": "main", "entrypoint": "run.py", "timeoutSeconds": 20}
    base.update(kw)
    return base


class PollTests(GitTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "install"

    def poll(self, cfg, *extra):
        return self.cli("poll", "--config", str(cfg), *extra)

    def test_two_experiments_clone_unchanged_update_and_ignored_paths(self):
        a_repo, a_seed = self.make_remote("alpha", {"run.py": RUN_PY})
        b_repo, _ = self.make_remote("beta", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", a_repo, ignorePaths=["results/"]),
                                            profile("beta", b_repo)])
        r = self.poll(cfg)
        self.assertEqual(r.returncode, 0, r.stdout)
        for exp in ("alpha", "beta"):
            self.assertEqual(self.state(self.root, exp)["status"], "succeeded")
            self.assertEqual(self.run_count(self.root, exp), 1)
        # Outputs went to the per-run dir, so the checkout stays clean.
        self.assertEqual(g(self.root / "workspaces" / "alpha", "status", "--porcelain"), "")

        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "unchanged")
        self.assertEqual(self.run_count(self.root, "alpha"), 1)

        new_sha = self.commit(a_seed, {"code.txt": "changed"}, "change alpha")
        self.poll(cfg)
        self.assertEqual(self.run_count(self.root, "alpha"), 2)
        self.assertEqual(self.run_count(self.root, "beta"), 1)
        self.assertEqual(self.state(self.root, "alpha")["commitSha"], new_sha)

        self.commit(a_seed, {"results/metrics.txt": "x"}, "results only")
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "ignored_change")
        self.assertEqual(self.run_count(self.root, "alpha"), 2)

        r = self.poll(cfg, "--only", "beta", "--force")
        self.assertEqual(self.run_count(self.root, "beta"), 2, r.stdout)

    def test_dirty_and_diverged_checkouts_are_refused_and_preserved(self):
        repo, seed = self.make_remote("alpha", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo)])
        self.poll(cfg)
        checkout = self.root / "workspaces" / "alpha"
        (checkout / "untracked.txt").write_text("keep me")
        r = self.poll(cfg, "--force")
        self.assertEqual(r.returncode, 1)
        st = self.state(self.root, "alpha")
        self.assertEqual(st["status"], "error")
        self.assertIn("GIT_POLLER_RUN_DIR", st["error"])
        self.assertTrue((checkout / "untracked.txt").exists())
        (checkout / "untracked.txt").unlink()

        g(checkout, *IDENTITY, "commit", "-q", "--allow-empty", "-m", "local")
        local_head = g(checkout, "rev-parse", "HEAD")
        self.commit(seed, {"code.txt": "remote"}, "remote change")
        self.poll(cfg)
        st = self.state(self.root, "alpha")
        self.assertEqual(st["status"], "error")
        self.assertIn("fast-forward", st["error"])
        self.assertEqual(g(checkout, "rev-parse", "HEAD"), local_head)

    def test_failure_is_recorded_and_not_retried_every_tick(self):
        repo, _ = self.make_remote("alpha", {"run.py": "import sys; sys.exit(3)"})
        cfg = self.write_config(self.root, [profile("alpha", repo)])
        self.assertEqual(self.poll(cfg).returncode, 1)
        st = self.state(self.root, "alpha")
        self.assertEqual((st["status"], st["exitCode"]), ("failed", 3))
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "unchanged")

    def test_timeout_kills_the_run(self):
        repo, _ = self.make_remote("alpha", {"run.py": "import time; time.sleep(60)"})
        cfg = self.write_config(self.root, [profile("alpha", repo, timeoutSeconds=2)])
        start = time.time()
        self.poll(cfg)
        self.assertLess(time.time() - start, 30)
        st = self.state(self.root, "alpha")
        self.assertEqual((st["status"], st["exitCode"]), ("timed_out", 124))

    def test_every_tick_mode_and_legacy_run_on_change_false(self):
        repo, _ = self.make_remote("alpha", {"run.py": RUN_PY})
        repo2, _ = self.make_remote("legacy", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo, runMode="every_tick"),
                                            profile("legacy", repo2, runOnChange=False)])
        self.poll(cfg)
        self.poll(cfg)
        self.assertEqual(self.run_count(self.root, "alpha"), 2)
        self.assertEqual(self.run_count(self.root, "legacy"), 2)
        self.assertIn("every_tick", self.cli("check", "--config", str(cfg)).stdout)

    def test_verification_result_is_separate_from_exit_code(self):
        repo, seed = self.make_remote("alpha", {"run.py": RUN_PY, "verify.py": "import sys; sys.exit(1)"})
        cfg = self.write_config(self.root, [profile("alpha", repo, verifyEntry="verify.py")])
        self.poll(cfg)
        st = self.state(self.root, "alpha")
        self.assertEqual((st["status"], st["verificationStatus"]), ("verification_failed", "failed"))
        self.commit(seed, {"verify.py": "pass"}, "fix verify")
        self.poll(cfg)
        st = self.state(self.root, "alpha")
        self.assertEqual((st["status"], st["verificationStatus"]), ("succeeded", "passed"))

    def test_entrypoint_must_stay_inside_checkout(self):
        repo, _ = self.make_remote("alpha", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo, entrypoint="../outside.py")])
        (self.root / "workspaces").mkdir(parents=True)
        (self.root / "workspaces" / "outside.py").write_text("print('escaped')")
        self.poll(cfg)
        self.assertIn("inside the approved checkout", self.state(self.root, "alpha")["error"])

    def test_busy_experiment_is_skipped_but_others_run(self):
        a_repo, _ = self.make_remote("alpha", {"run.py": RUN_PY})
        b_repo, _ = self.make_remote("beta", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", a_repo), profile("beta", b_repo)])
        lock = FileLock(self.root / "state" / "alpha.lock")
        self.assertTrue(lock.acquire())
        try:
            r = self.poll(cfg)
        finally:
            lock.release()
        self.assertIn("alpha: busy", r.stdout)
        self.assertEqual(self.run_count(self.root, "alpha"), 0)
        self.assertEqual(self.run_count(self.root, "beta"), 1)

    def test_long_experiment_does_not_block_another(self):
        slow, _ = self.make_remote("slow", {"run.py": "import time; time.sleep(6)"})
        fast, _ = self.make_remote("fast", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("slow", slow), profile("fast", fast)])
        t = threading.Thread(target=self.poll, args=(cfg,))
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and self.run_count(self.root, "fast") == 0:
            time.sleep(0.2)
        self.assertEqual(self.run_count(self.root, "fast"), 1, "fast experiment waited for slow one")
        t.join()
        self.assertEqual(self.state(self.root, "slow")["status"], "succeeded")

    def test_interrupted_run_is_visible_and_needs_force(self):
        repo, _ = self.make_remote("alpha", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo)])
        self.poll(cfg)
        path = self.root / "state" / "alpha.json"
        st = json.loads(path.read_text())
        st["status"] = "running"  # what a crash/reboot leaves behind
        path.write_text(json.dumps(st))
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "interrupted")
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "interrupted")
        self.poll(cfg, "--force")
        self.assertEqual(self.state(self.root, "alpha")["status"], "succeeded")

    def test_offline_remote_then_resume(self):
        repo, seed = self.make_remote("alpha", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo)])
        self.poll(cfg)
        self.commit(seed, {"code.txt": "v2"}, "v2")
        hidden = Path(repo + ".offline")
        shutil.move(repo, hidden)
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "error")
        shutil.move(str(hidden), repo)
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "succeeded")
        self.assertEqual(self.run_count(self.root, "alpha"), 2)

    def test_wrong_remote_is_refused(self):
        repo, _ = self.make_remote("alpha", {"run.py": RUN_PY})
        other, _ = self.make_remote("other", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo)])
        self.poll(cfg)
        cfg = self.write_config(self.root, [profile("alpha", other)])
        self.poll(cfg)
        self.assertIn("Wrong Git remote", self.state(self.root, "alpha")["error"])

    def test_paused_flag_stops_everything(self):
        repo, _ = self.make_remote("alpha", {"run.py": RUN_PY})
        cfg = self.write_config(self.root, [profile("alpha", repo)])
        (self.root / "paused.flag").write_text("paused")
        self.assertIn("paused", self.poll(cfg).stdout)
        self.assertFalse((self.root / "workspaces" / "alpha").exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_powershell_entrypoint(self):
        ps = 'Set-Content -LiteralPath (Join-Path $env:GIT_POLLER_RUN_DIR "marker.txt") -Value $env:GIT_POLLER_COMMIT'
        repo, _ = self.make_remote("alpha", {"run.ps1": ps})
        cfg = self.write_config(self.root, [profile("alpha", repo, entrypoint="run.ps1")])
        self.poll(cfg)
        self.assertEqual(self.state(self.root, "alpha")["status"], "succeeded")
        self.assertEqual(self.run_count(self.root, "alpha"), 1)


# Benchmark-style experiment: writes results INTO the checkout (declared resultPaths).
BENCH_PY = """import os, pathlib
here = pathlib.Path(__file__).parent
out = here / "results" / "runs"
out.mkdir(parents=True, exist_ok=True)
n = len(list(out.glob("*.txt")))
(out / f"result {n}.txt").write_text(f"result {n}")
(pathlib.Path(os.environ["GIT_POLLER_RUN_DIR"]) / "marker.txt").write_text("run")
"""


class ResultSyncTests(GitTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "install"
        self.repo, self.seed = self.make_remote("bench", {"run.py": BENCH_PY, "code.txt": "v1"})
        self.cfg = self.write_config(self.root, [profile(
            "bench", self.repo, ignorePaths=["results/runs/"], resultPaths=["results/runs/", "results/logs/"])])
        self.checkout = self.root / "workspaces" / "bench"

    def poll(self):
        return self.cli("poll", "--config", str(self.cfg))

    def test_results_are_committed_pushed_and_rebased_without_rerun(self):
        self.poll()
        self.assertTrue((self.checkout / "results" / "runs" / "result 0.txt").exists())
        g(self.checkout, "config", "user.name", "Bench")
        g(self.checkout, "config", "user.email", "bench@example.test")

        r = self.poll()  # pending results committed + pushed, no rerun
        # Our own result commit is a change, but only under ignored paths.
        self.assertEqual(self.state(self.root, "bench")["status"], "ignored_change", r.stdout)
        self.assertEqual(self.run_count(self.root, "bench"), 1)
        g(self.seed, "pull", "-q", "origin", "main")
        self.assertTrue((self.seed / "results" / "runs" / "result 0.txt").exists())

        # Remote code change while a new result is pending -> rebase + rerun once.
        (self.checkout / "results" / "runs" / "result 1.txt").write_text("pending")
        self.commit(self.seed, {"code.txt": "v2"}, "code v2")
        self.poll()
        self.assertEqual(self.state(self.root, "bench")["status"], "succeeded")
        self.assertEqual(self.run_count(self.root, "bench"), 2)
        g(self.seed, "pull", "-q", "origin", "main")
        self.assertTrue((self.seed / "results" / "runs" / "result 1.txt").exists())

    def test_local_code_changes_are_never_committed(self):
        self.poll()
        g(self.checkout, "config", "user.name", "Bench")
        g(self.checkout, "config", "user.email", "bench@example.test")
        (self.checkout / "code.txt").write_text("local edit")
        self.poll()
        st = self.state(self.root, "bench")
        self.assertEqual(st["status"], "error")
        self.assertIn("Non-result changes", st["error"])
        self.assertEqual((self.checkout / "code.txt").read_text(), "local edit")
        self.assertEqual(g(self.checkout, "rev-list", "--count", "origin/main..HEAD"), "0")

    def test_unsafe_result_paths_rejected(self):
        self.poll()
        for bad in ("../x", "/abs", "C:/x", "res*/", "."):
            r = self.cli("sync-results", "--repo", str(self.checkout), "--branch", "main",
                         "--expected-remote", self.repo, "--result-path", bad)
            self.assertEqual(r.returncode, 1, bad)


if __name__ == "__main__":
    unittest.main()
