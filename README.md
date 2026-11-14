# Git Repo Poller

A **small Windows PowerShell 7 polling/bootstrap tool**. Its one job is to keep approved Git experiment checkouts current and invoke their own `run.ps1`. It does not need Docker, Python, a GPU, an LLM or the Nexus mailbox.

**Status:** MVP implementation proposed in a PR; not yet tested on a real Windows machine. The user-facing setup and Task Scheduler path require Windows testing before enabling unattended experiments.

## Quick setup (Windows)

Prerequisites: PowerShell 7 and Git for Windows, with credentials for any **private experiment repository**. Public poller repo:

```powershell
git clone https://github.com/tire0011/git-repo-poller.git
cd git-repo-poller
pwsh -File .\setup.ps1
```

The wizard asks for a worker ID, Git repository/branch, a local `run.ps1` entrypoint, timeout, paths to ignore (e.g. generated results), polling interval, automatic poller updates and optional **current-user Windows Task Scheduler** registration. Only select trusted repositories whose scripts you intend to execute locally.

Nothing is run automatically during installation. Before enabling scheduled runs, verify that the experiment script does not launch its own infinite watcher (the existing AI-Native Code Bench `run.ps1` starts a watcher unless called with `-NoWatch`; use the `poller-run.ps1` adapter from [the source migration PR](https://github.com/tire0011/ai-native-code-bench/pull/4)).

For the source benchmark use `entrypoint: poller-run.ps1` (not raw `run.ps1`). The adapter reuses `run.ps1 -NoWatch` and its existing result sync; see [the migration/parity guide](https://github.com/tire0011/ai-native-code-bench/blob/refactor/benchmark-poller-adapter/docs/git-repo-poller-migration.md). The generic polling engine was ported from the benchmark watcher but still needs this real-project integration before it can replace the original watcher. Old and new watchers must never run concurrently.

### Commands

```powershell
# Review configuration without changing anything:
pwsh -File .\setup.ps1 -DryRun

# Register an additional experiment interactively:
pwsh -File .\setup.ps1 -AddExperiment

# Manual poll: updates Git and runs changed experiments:
pwsh -File "$env:LOCALAPPDATA\GitRepoPoller\launcher.ps1"

# Run a specific experiment again (explicit request):
pwsh -File "$env:LOCALAPPDATA\GitRepoPoller\launcher.ps1" -Only local-benchmark -Force

# Pause / resume without removing workspaces or history:
pwsh -File .\setup.ps1 -Pause
pwsh -File .\setup.ps1 -Resume

# Repair installation or uninstall launcher/task (keep workspaces/results):
pwsh -File .\setup.ps1 -Repair
pwsh -File .\setup.ps1 -Uninstall
```

The local-only `config.json` is stored under `%LOCALAPPDATA%\GitRepoPoller` (not in the public repository). For scripted installs:

```powershell
pwsh -File .\setup.ps1 -NonInteractive -ConfigPath .\examples\config.example.json -DryRun
# Remove -DryRun only after adapting the repository URLs and entrypoints.
```

## How it works

```text
Task Scheduler (optional)
    └── installed launcher.ps1 (stable; never self-overwritten during execution)
          ├── fetch/fast-forward poller engine, read-only check + keep last known good snapshot
          └── poll.ps1 + optional benchmark-derived sync-results.ps1
                ├── experiment A: fetch + ff-only + approved run.ps1
                └── experiment B: fetch + ff-only + approved run.ps1

Local to installation: config.json, state/*.json, logs/, workspaces/, versions/
```

- A single PowerShell iteration checks every configured repository. A local mutex prevents overlapping ticks.
- Existing worktrees must have the exact configured `origin`, branch and a **clean** working tree. No reset, force-push or destructive cleanup. An **optional** extracted results-only reconciliation may safely rebase result commits, never code commits.
- A first run executes once; later runs execute only if a new commit changes paths outside `ignorePaths`. `-Force` explicitly reruns one or all experiments.
- No automatic rerun of a failed job on every tick; a failed commit is recorded and must be retried manually or after a new change.
- A fixed `.ps1` entrypoint inside the checkout runs with a timeout in a separate PowerShell process. `verifyEntry` is optional. Logs/state stay **outside** the experiment checkout in the poller installation; scripts can write to `$env:GIT_POLLER_RUN_DIR`.
- **Reused from AI-Native Code Bench:** `sync-results.ps1` is adapted from the existing benchmark script. If an experiment explicitly sets `resultPaths`, the poller commits/publishes only those result files **before** checking for new code. It may rebase **results-only** local commits onto remote updates; it refuses commits touching other paths. With no `resultPaths`, the poller never commits, rebases, or pushes an experiment repository.
- The benchmark's existing `run.ps1` and `sync-results.ps1` remain responsible for generating/publishing its outputs during a run; `poller-run.ps1` calls the existing runner with `-NoWatch`. The extracted pre-poll sync recovers pending results after interruptions, without creating a second benchmark runner. Never upload internal/company information to public GitHub.
- The poller engine can update independently of experiments between ticks. The launcher snapshots validated engine scripts in `versions/`. Old snapshots are kept for manual recovery; there is no hot code patching.

## Result synchronization (opt-in)

Each experiment is selected through configuration, not hard-coded watcher logic:

```json
{
  "id": "local-benchmark",
  "repo": "https://github.com/tire0011/ai-native-code-bench.git",
  "branch": "main",
  "entrypoint": "poller-run.ps1",
  "runOnChange": true,
  "resultPaths": ["results/runs/", "results/logs/"],
  "ignorePaths": ["results/runs/", "results/logs/"],
  "timeoutSeconds": 3600
}
```

Set `resultPaths: []` (or omit the property) for experiments that never publish results to their source repository. For the source benchmark, use [AI-Native Code Bench PR #4](https://github.com/tire0011/ai-native-code-bench/pull/4) to provide `poller-run.ps1`. It passes `-NoWatch` to the **original** `run.ps1` so the old foreground watch loop is never restarted.

**Provenance:** The result synchronizer is a direct, parameterized adaptation of [the original benchmark `sync-results.ps1`](https://github.com/tire0011/ai-native-code-bench/blob/main/sync-results.ps1). The polling loop reuses its checks for trusted remote/branch, mutex, fast-forward updates and change detection. A single generic poller can now run this benchmark or any other approved experiment without needing Nexus or an AI model in the poller itself.

## Offline synthetic verification

```powershell
pwsh -File .\tests\Smoke.Tests.ps1
```

Creates **temporary local bare Git remotes**: two independent experiments, update filtering, and the extracted benchmark results-only commit/push/rebase lifecycle (including a remote code update while local result files are pending and protection of local code commits). No network, paid models or cloud services. The temporary remotes are removed afterward.

For the source benchmark extraction and compatibility plan see [ai-native-code-bench#3](https://github.com/tire0011/ai-native-code-bench/issues/3); implementation task is [#1](https://github.com/tire0011/git-repo-poller/issues/1).

## Boundaries

The **Mailbox** delivers tasks/replies and can separately provide an owner-enabled CommandHandler. This **Git Repo Poller** only invokes locally configured experiment scripts. Do not add a second mailbox, remote free-form command execution, or an agent framework to this project.

[MIT License](LICENSE) © 2026 Rene Tittel.
