# Git Repo Poller

A **small Git poller** that keeps approved experiment checkouts current and runs each experiment's own entrypoint when its repository changes. The implementation is **Python 3.10+ (standard library only)**; `poller.ps1` is the single PowerShell wrapper for Windows and Task Scheduler.

The idea: experiments are prepared as Git repositories (by you or by agents), and a worker machine picks them up and runs them locally. The poller needs no GPU, AI model or mailbox. Each experiment owns its commands, dependencies and models.

**Status:** implemented and covered by offline tests on Linux (Python and the PowerShell wrapper). CI runs the same tests on Windows. Task Scheduler registration has not been verified on a real Windows machine yet.

```text
Windows Task Scheduler (optional, opt-in)
  └── poller.ps1 launch           (stable bootstrap copy in the install folder)
        ├── optional engine self-update between ticks, last-known-good fallback
        └── poll tick (one thread + one lock per experiment)
              ├── experiment A: fetch, ff-only, run.py / run.ps1 → runs/A/<time>-<sha>/
              └── experiment B: fetch, ff-only, run.py / run.ps1 → runs/B/<time>-<sha>/
```

## Trust model (read first)

**Anyone who can push to a configured branch can run code on this machine**, as your user and with your Git credentials. This holds for every experiment repository, and for the poller repository itself if `autoUpdate` is on.

- Protect the configured branches (branch protection on `main`). Agents deliver experiments via pull requests; a human merges.
- `autoUpdate` is **off by default**. Turn it on only for a poller remote you control.
- The engine check before switching versions is a startup/config check, **not** a security gate: it already executes the new code.

## Setup (Windows)

Prerequisites: Git for Windows, PowerShell 7, Python 3.10+ (`winget install Git.Git`, `winget install Microsoft.PowerShell`, `winget install Python.Python.3.12`), and credentials for private experiment repositories.

```powershell
git clone https://github.com/tire0011/git-repo-poller.git
cd git-repo-poller
pwsh -File .\poller.ps1 setup
```

The wizard asks for a worker ID, polling interval, self-update (default no) and experiments, then optionally registers a **current-user** scheduled task (default no). Nothing runs during setup. Everything is installed under `%LOCALAPPDATA%\GitRepoPoller`.

```powershell
$p = "$env:LOCALAPPDATA\GitRepoPoller\poller.ps1"
pwsh -File $p check --config "$env:LOCALAPPDATA\GitRepoPoller\config.json"  # validate only
pwsh -File $p launch                                   # one tick
pwsh -File $p launch --only local-benchmark --force    # explicit rerun of one experiment
pwsh -File .\poller.ps1 setup --add-experiment
pwsh -File .\poller.ps1 setup --pause                  # / --resume
pwsh -File .\poller.ps1 setup --repair                 # reinstall wrapper + bootstrap from installed config
pwsh -File .\poller.ps1 setup --uninstall              # keeps workspaces, runs, state, engine
pwsh -File .\poller.ps1 setup --non-interactive --config .\examples\config.example.json --dry-run
```

Without PowerShell (any OS): `python -m git_repo_poller <command> ...` from the repository folder.

## Configuration

```json
{
  "workerId": "home-gpu",
  "intervalMinutes": 5,
  "autoUpdate": false,
  "engineRemote": "https://github.com/tire0011/git-repo-poller.git",
  "engineBranch": "main",
  "experiments": [
    {
      "id": "demo",
      "repo": "https://github.com/you/your-experiment.git",
      "branch": "main",
      "entrypoint": "run.py",
      "verifyEntry": "verify.py",
      "timeoutSeconds": 600,
      "runMode": "on_change",
      "ignorePaths": ["docs/"],
      "resultPaths": []
    }
  ]
}
```

- `entrypoint` / `verifyEntry`: a `.py` (run with the poller's Python) or `.ps1` (run with `pwsh`) file **inside** the checkout.
- `runMode`: `on_change` (default) runs on the first check and after commits that touch paths outside `ignorePaths`/`resultPaths`. `every_tick` runs on every tick. The old `runOnChange: false` is read as `every_tick`.
- `resultPaths` (opt-in): result folders that are committed and pushed **before** each update. Only result-only commits are ever rebased or pushed. Empty means the poller never commits or pushes.

## What an experiment gets

| Variable | Meaning |
| --- | --- |
| `GIT_POLLER_RUN_DIR` | Fresh folder for this run (outside the checkout). Logs `run.out.log`, `run.err.log` go here. **Write outputs here.** |
| `GIT_POLLER_EXPERIMENT_ID`, `GIT_POLLER_COMMIT`, `GIT_POLLER_WORKER_ID` | Context of this run |

Files written into the checkout make it dirty; the poller then refuses to update it (unless they are under `resultPaths`).

Status per experiment is in `state/<id>.json`: `succeeded`, `failed`, `timed_out`, `verification_failed`, `unchanged`, `ignored_change`, `busy`, `interrupted` (machine crashed mid-run; rerun with `--force`), `error`. Exit code 0 means "executed", not "valid": use `verifyEntry` for that. A failed commit is not retried every tick; it needs a new commit or `--force`.

## Safety rules

- Never `git reset --hard`, force-push or overwrite dirty/diverged checkouts; they are reported as `error` and left untouched.
- The configured `origin` and branch must match exactly.
- Experiments run in parallel, each with its own lock and timeout (the whole process tree is killed on timeout). Overlapping ticks skip busy experiments.
- Scheduled tasks, self-update and result publishing are opt-in.

## AI Native Code Bench

Use `entrypoint: poller-run.ps1` with `resultPaths`/`ignorePaths` `results/runs/`, `results/logs/` (see `examples/config.example.json` and [ai-native-code-bench#3](https://github.com/tire0011/ai-native-code-bench/issues/3)). The result sync is a port of the benchmark's `sync-results.ps1`. Never run the old watcher and the poller at the same time.

## Tests

```bash
python -m unittest discover -v -s tests -t .
```

Offline only: temporary local bare Git remotes and dummy entrypoints. Covers two experiments, updates, unchanged/ignored commits, dirty and diverged checkouts, failures, timeouts, verification, parallel runs and locks, interrupted runs, offline resume, result sync, installer modes, self-update with fallback and the PowerShell wrapper (if `pwsh` is installed).

## Boundaries

The [Mailbox](https://github.com/tire0011/agent-lab-mailbox) delivers tasks and replies; this poller only runs locally configured experiments. A future mailbox adapter may request only locally approved experiment IDs. No remote free-form command execution.

[MIT License](LICENSE) © 2026 Rene Tittel.
