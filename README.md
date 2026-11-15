# Git Repo Poller

A **small Git poller** that keeps one approved experiment checkout current and runs the experiment's own entrypoint when its repository changes. The implementation is **Python 3.10+ (standard library only)**; `poller.ps1` is the single PowerShell wrapper. It runs as a normal app in a terminal and checks for changes every 30 seconds (no Task Scheduler, no service).

**One experiment per instance.** Each poller installation (its own folder + `config.json`) runs exactly one Git-based experiment. To run several experiments, install several independent pollers with different configurations — they never share workspaces, locks, runs or state.

The idea: experiments are prepared as Git repositories (by you or by agents), and a worker machine picks them up and runs them locally. The poller needs no GPU, AI model, mailbox or agent framework. Each experiment owns its commands, dependencies and models.

**Status:** implemented and covered by offline tests on Linux (Python and the PowerShell wrapper). CI runs the same tests on Windows. Not yet tried on a real Windows worker machine.

```text
poller.ps1 watch                  (runs until Ctrl+C; one tick every intervalSeconds)
  └── launch                      (stable bootstrap copy in the install folder)
        ├── optional engine self-update between ticks, last-known-good fallback
        └── poll tick (one lock)
              └── experiment: fetch, ff-only, run.py / run.ps1 → runs/<id>/<time>-<sha>/
```

## Trust model (read first)

**Anyone who can push to the configured branch can run code on this machine**, as your user and with your Git credentials. This holds for the experiment repository, and for the poller repository itself if `autoUpdate` is on.

- Protect the configured branch (branch protection on `main`). Agents deliver experiments via pull requests; a human merges.
- `autoUpdate` is **off by default**. Turn it on only for a poller remote you control.
- The engine check before switching versions is a startup/config check, **not** a security gate: it already executes the new code.

## Setup (Windows)

Prerequisites: Git for Windows, PowerShell 7, Python 3.10+ (`winget install Git.Git`, `winget install Microsoft.PowerShell`, `winget install Python.Python.3.12`), and credentials for private experiment repositories.

```powershell
git clone https://github.com/tire0011/git-repo-poller.git
cd git-repo-poller
pwsh -File .\poller.ps1 setup
```

The wizard asks for a worker ID, polling interval, self-update (default no) and **the one experiment** (repository, branch, entrypoint, timeout, result paths). Nothing runs during setup. Everything is installed under `%LOCALAPPDATA%\GitRepoPoller`.

```powershell
$p = "$env:LOCALAPPDATA\GitRepoPoller\poller.ps1"
pwsh -File $p check --config "$env:LOCALAPPDATA\GitRepoPoller\config.json"  # validate only
pwsh -File $p watch                                    # keep running, check every 30 s (Ctrl+C stops)
pwsh -File $p launch                                   # one tick
pwsh -File $p launch --force                           # explicit rerun of the experiment
pwsh -File .\poller.ps1 setup                          # change the configured experiment (interactive)
pwsh -File .\poller.ps1 setup --non-interactive --config .\examples\config.example.json   # replace experiment unattended
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
  "intervalSeconds": 30,
  "autoUpdate": false,
  "engineRemote": "https://github.com/tire0011/git-repo-poller.git",
  "engineBranch": "main",
  "experiment": {
    "id": "local-benchmark",
    "repo": "https://github.com/you/your-experiment.git",
    "branch": "main",
    "entrypoint": "run.py",
    "verifyEntry": "verify.py",
    "timeoutSeconds": 600,
    "runMode": "on_change",
    "ignorePaths": ["docs/"],
    "resultPaths": []
  }
}
```

- One `experiment` object per config. A legacy `"experiments": [...]` list with **exactly one** entry is still accepted and auto-migrated (run `setup` once to rewrite the installed config); a list with several entries is refused — split it into one installation per experiment.
- `intervalSeconds`: pause between ticks in `watch` (default 30, minimum 5; older `intervalMinutes` is converted).
- `entrypoint` / `verifyEntry`: a `.py` (run with the poller's Python) or `.ps1` (run with `pwsh`) file **inside** the checkout.
- `runMode`: `on_change` (default) runs on the first check and after commits that touch paths outside `ignorePaths`/`resultPaths`. `every_tick` runs on every tick. The old `runOnChange: false` is read as `every_tick`.
- `resultPaths` (opt-in): result folders that are committed and pushed **before** each update. Only result-only commits are ever rebased or pushed. Empty means the poller never commits or pushes.

### Multiple workers / experiments

Each installation is independent: give each its own `--install-root` (e.g. `%LOCALAPPDATA%\GitRepoPoller-bench`, `...-demo`) and its own config with one experiment. They can run side by side; each has its own lock, so overlapping ticks only skip within the same installation.

### Changing the experiment

Run `setup` again (interactive: answer "yes" to *Replace the configured experiment?*, or pass `--non-interactive --config new.json`). Rules:

- The previous experiment's workspace (`workspaces/<old-id>`), runs (`runs/<old-id>`), state (`state/<old-id>.json`) and logs are **never** overwritten or deleted — directories are keyed by experiment id.
- Switching is refused while an execution is still active (the running experiment holds its lock); the poller also skips a tick if any other execution is active.
- Reusing an experiment id for a different repository is refused (the existing workspace belongs to another remote); pick a new id.

## What an experiment gets

| Variable | Meaning |
| --- | --- |
| `GIT_POLLER_RUN_DIR` | Fresh folder for this run (outside the checkout). Logs `run.out.log`, `run.err.log` go here. **Write outputs here.** |
| `GIT_POLLER_EXPERIMENT_ID`, `GIT_POLLER_COMMIT`, `GIT_POLLER_WORKER_ID` | Context of this run |

Files written into the checkout make it dirty; the poller then refuses to update it (unless they are under `resultPaths`).

Status is in `state/<id>.json`: `succeeded`, `failed`, `timed_out`, `verification_failed`, `unchanged`, `ignored_change`, `busy`, `interrupted` (machine crashed mid-run; rerun with `--force`), `error`. Exit code 0 means "executed", not "valid": use `verifyEntry` for that. A failed commit is not retried every tick; it needs a new commit or `--force`.

## Safety rules

- Never `git reset --hard`, force-push or overwrite dirty/diverged checkouts; they are reported as `error` and left untouched.
- The configured `origin` and branch must match exactly.
- One lock per installation: overlapping ticks are skipped, and the whole process tree is killed on timeout.
- Self-update and result publishing are opt-in. Nothing runs in the background: the poller runs only while `watch` is open.

## AI Native Code Bench

Use `entrypoint: poller-run.ps1` with `resultPaths`/`ignorePaths` `results/runs/`, `results/logs/` (see `examples/config.example.json` and [ai-native-code-bench PR #4](https://github.com/tire0011/ai-native-code-bench/pull/4)). That PR adds `poller-run.ps1`, the benchmark's **one-shot adapter**: it calls the existing `run.ps1 -NoWatch`, so the legacy watcher is never started recursively. Configure the poller with `poller-run.ps1`, never with `run.ps1` directly. The result sync is a port of the benchmark's `sync-results.ps1`. Never run the old watcher and the poller at the same time; the existing benchmark watcher stays paused.

## Tests

```bash
python -m unittest discover -v -s tests -t .
```

Offline only: temporary local bare Git remotes and dummy entrypoints. Covers initial clone, updates, unchanged/ignored revisions, dirty and diverged checkouts, failures, timeouts, verification, locks and busy ticks, interrupted runs, offline resume, result sync, two independent installations, legacy config migration, experiment replacement, installer modes, the `watch` loop, self-update with fallback and the PowerShell wrapper (if `pwsh` is installed).

## Boundaries

The [Mailbox](https://github.com/tire0011/agent-lab-mailbox) delivers tasks and replies; this poller only runs the locally configured experiment. No remote free-form command execution, no plugin framework, no agent orchestration.

[MIT License](LICENSE) © 2026 Rene Tittel.
