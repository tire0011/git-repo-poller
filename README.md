# Git Repo Poller

**One poller installation, one Git experiment.** This small Python 3.10+ standard-library tool checks a trusted Git repository and runs its configured `run.py` or `run.ps1` when relevant commits appear. `poller.ps1` is the Windows PowerShell wrapper. No Mailbox, Nexus, MCP, GPU, model or plugin framework is required.

Each installation has its own configuration, lock, checkout, execution history and logs. To run two experiments, create **two separate installations** with different `--install-root` values. One installation never runs multiple experiments.

## Setup (Windows)

Prerequisites: Git for Windows, Python 3.10+, PowerShell 7 (for the wrapper and `.ps1` experiments). The poller uses the user's existing Git credentials for private repositories.

```powershell
git clone https://github.com/tire0011/git-repo-poller.git
cd git-repo-poller
pwsh -File .\poller.ps1 setup
```

The interactive wizard selects one repository, branch, experiment ID, entrypoint, interval, timeout and optional result directories. `autoUpdate` is off by default. No experiment is run by setup. Default install root is `$env:LOCALAPPDATA\GitRepoPoller`.

```powershell
$p = "$env:LOCALAPPDATA\GitRepoPoller\poller.ps1"
pwsh -File $p check --config "$env:LOCALAPPDATA\GitRepoPoller\config.json"
pwsh -File $p launch                       # one Git check, one experiment at most
pwsh -File $p launch --force               # rerun the same experiment explicitly
pwsh -File $p watch                        # repeat while terminal is open; Ctrl+C stops

# Switch to another experiment in the SAME installation (old files retained):
pwsh -File .\poller.ps1 setup --replace-experiment

# Set up a SECOND, independent poller installation:
pwsh -File .\poller.ps1 setup --install-root "$env:LOCALAPPDATA\GitRepoPoller-2"

pwsh -File $p setup --pause
pwsh -File $p setup --resume
pwsh -File $p setup --repair
pwsh -File $p setup --uninstall             # does not delete workspaces/runs
```

The change command refuses to replace the configuration during an active run. Use a **new experiment ID** when changing repository or branch; existing workspaces, result files and run logs remain on disk.

`watch` runs in the terminal until stopped; it does **not** install a Windows service or Task Scheduler job, and does not automatically restart after reboot. If needed, arrange startup separately after verifying the host.

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
    "repo": "https://github.com/tire0011/ai-native-code-bench.git",
    "branch": "main",
    "entrypoint": "poller-run.ps1",
    "timeoutSeconds": 3600,
    "runMode": "on_change",
    "ignorePaths": ["results/runs/", "results/logs/"],
    "resultPaths": ["results/runs/", "results/logs/"]
  }
}
```

For automation or a second installation, supply this object with:

```powershell
pwsh -File .\poller.ps1 setup --install-root "$env:LOCALAPPDATA\GitRepoPoller-2" --non-interactive --config .\examples\config.example.json --dry-run
```

`--dry-run` validates without writing or executing. Remove it only after adapting the remote and paths.

**Legacy migration:** The old `"experiments": [ { ... } ]` format is accepted only when it contains **exactly one** experiment. Multi-experiment configurations fail with an explanation; create separate installations rather than silently losing jobs.

## How each tick works

1. Ensure the configured checkout exists, verify its remote/branch and protect local changes.
2. Optionally reconcile only `resultPaths` using the tested result-only commit/push/rebase logic adapted from [AI-Native Code Bench](https://github.com/tire0011/ai-native-code-bench/blob/main/sync-results.ps1).
3. Fetch Git updates and fast-forward safely. If a code commit changed outside ignored/result paths, invoke the configured entrypoint once.
4. Save status and per-run stdout/stderr under `runs/<experiment-id>/`; the entrypoint also gets `GIT_POLLER_RUN_DIR` to write results outside its checkout.

`runMode`: `on_change` (default), `every_tick` or `service` (see below). No forced replay after a crashed run: the interrupted commit is reported, and a rerun requires `--force`. One installation-wide file lock prevents overlapping runs and setup changes. `verifyEntry` can separately verify a successful execution; exit code 0 alone is not proof of result validity.

`resultPaths` defaults to empty, meaning **no automatic Git commits, rebases or pushes**. The poller never uses `git reset --hard` or force-push, and refuses dirty non-result worktrees.

The stable launcher may optionally update the poller engine *between* ticks, retaining a last-known-good snapshot. `autoUpdate` is off by default; turning it on trusts new code from the configured engine branch.

## Long-running agents (`runMode: "service"`)

For an entrypoint that runs until stopped (for example an agent bridge), set `"runMode": "service"`. `launch`/`watch` then supervise it instead of waiting for it to exit:

1. Fetch and fast-forward the checkout, then start the entrypoint in the background (logs in `runs/<experiment-id>/<stamp>-<sha>/`).
2. Every `intervalSeconds`, fetch. When a commit changes files outside `ignorePaths`/`resultPaths`, stop the process, fast-forward, then start it again on the new commit. Commits that change only ignored paths are fast-forwarded without a restart.
3. If the process exits on its own, the exit is recorded (`exited`/`failed`) and it is **not** restarted until the next relevant commit, so a crashing agent cannot loop.
4. If the checkout is offline, dirty or diverged, the running process is left alone and the problem goes into the state file's `error` field.
5. `setup --pause` (from another terminal) or Ctrl+C stops the process and the supervisor.

Stopping sends Ctrl+Break (Windows) or SIGTERM (Linux), waits `stopTimeoutSeconds` (default 30), then kills the remaining process tree. On Windows the process tree is in a job object, so killing the poller also stops the agent and leaves no unsupervised duplicate. `timeoutSeconds` and `verifyEntry` do not apply in service mode. Because the supervisor holds the installation lock while the agent runs, the engine self-update and `setup --replace-experiment` wait until it is stopped. `state/<experiment-id>.json` shows `status` (`running`, `restarting`, `exited`, `failed`, `stopped`), `pid`, `commitSha` and `restarts`.

### Multi-Agent Collaboration Hub

[`examples/config.hub.example.json`](examples/config.hub.example.json) runs one hub agent's bridge via the hub's own `start.ps1`, which starts the single agent configured under `%LOCALAPPDATA%\agent-hub\agents\`. Agent configuration and credentials stay in that per-user directory, outside the checkout.

```powershell
pwsh -File .\poller.ps1 setup --install-root "$env:LOCALAPPDATA\GitRepoPoller-hub" --non-interactive --config .\examples\config.hub.example.json
# start.ps1 needs a .venv inside the poller's checkout. Create it once after the first clone:
pwsh -File "$env:LOCALAPPDATA\GitRepoPoller-hub\poller.ps1" launch --install-root "$env:LOCALAPPDATA\GitRepoPoller-hub"   # clones; stop with Ctrl+C
cd "$env:LOCALAPPDATA\GitRepoPoller-hub\workspaces\agent-hub"
py -3.11 -m venv .venv; .\.venv\Scripts\python -m pip install -e .
pwsh -File "$env:LOCALAPPDATA\GitRepoPoller-hub\poller.ps1" watch --install-root "$env:LOCALAPPDATA\GitRepoPoller-hub"
```

The hub supports **one bridge process per SQLite database**, so do not also run `start.ps1` for the same agent from another checkout. The editable install picks up code changes on restart. Dependency changes in `pyproject.toml` still need `pip install -e .` again.

## AI-Native Code Bench

The existing benchmark's `run.ps1` starts its own watcher unless called with `-NoWatch`. The companion [benchmark adapter PR #4](https://github.com/tire0011/ai-native-code-bench/pull/4) supplies a one-shot `poller-run.ps1` which invokes the original `run.ps1 -NoWatch` and preserves its existing result publishing. Merge and test that adapter before selecting the benchmark as a live experiment. Never run the old watcher and this poller simultaneously.

## Tests

```powershell
python -m unittest discover -v -s tests -t .
```

All tests use synthetic local Git repositories and dummy entrypoints: setup and migration, single-experiment change detection, two **separate installations**, locking, updates, failures, timeout, crash recovery and result sync. GitHub Actions also runs Windows and Linux tests.

## Trust boundary

Anyone who can push to the selected experiment branch can cause scripts to run as the local user. Limit write access to trusted people/agents. The poller has **no mailbox integration or remote-command interface**.

MIT License © 2026 Rene Tittel.
