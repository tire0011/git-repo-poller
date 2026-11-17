# Git Repo Poller

Watches **one** Git repository. When new commits arrive, it pulls them and starts the repository's program again. That program can be a long-running agent that gets restarted, or a script that runs once per change. Python 3.10+ standard library only; `poller.ps1` is the Windows wrapper.

## Quick start (Windows)

Prerequisites: Git for Windows, Python 3.10+, PowerShell 7. Private repositories use your existing Git credentials.

```powershell
git clone https://github.com/tire0011/git-repo-poller.git
.\git-repo-poller\poller.ps1
```

The first run asks five questions. Press Enter to accept a `[default]`:

1. Git repository URL
2. Branch
3. Is it a long-running program (e.g. an agent) that should be restarted on every change?
4. Program to start inside the repository (`.ps1` or `.py`)
5. How often to check for changes

Then it runs: it clones, starts the program, checks for new commits, pulls them and restarts the program. **Ctrl+C stops everything.** Next time, `.\git-repo-poller\poller.ps1` starts straight away.

That's all you need. The other commands are:

```powershell
.\git-repo-poller\poller.ps1 status      # what is running, last result, log file
.\git-repo-poller\poller.ps1 setup       # change the settings (asks the questions again)
.\git-repo-poller\poller.ps1 setup --pause | --resume | --uninstall
```

Notes:
- Settings, clone and logs live in `$env:LOCALAPPDATA\GitRepoPoller`. Finer settings such as `ignorePaths`, `timeoutSeconds` or `every_tick` are in `config.json` there (see below).
- It runs while the terminal is open. It does **not** install a Windows service or scheduled task, and does not start again after a reboot.
- Settings cannot be changed while the program is running; stop it first with Ctrl+C.
- If you switch to another repository, the old clone and logs stay on disk. A different repository with the same name gets a new ID (e.g. `name-2`).
- To watch a **second** repository, use a separate installation: `poller.ps1 start --install-root "$env:LOCALAPPDATA\GitRepoPoller-2"` (and the same `--install-root` for `setup`/`status`). One installation never runs more than one program.

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

For unattended setup, supply this object with:

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

Answering **yes** to "long-running program?" in the setup questions selects `"runMode": "service"`. The poller then supervises the program instead of waiting for it to exit:

1. Fetch and fast-forward the checkout, then start the entrypoint in the background (logs in `runs/<experiment-id>/<stamp>-<sha>/`).
2. Every `intervalSeconds`, fetch. When a commit changes files outside `ignorePaths`/`resultPaths`, stop the process, fast-forward, then start it again on the new commit. Commits that change only ignored paths are fast-forwarded without a restart.
3. If the process exits on its own, the exit is recorded (`exited`/`failed`) and it is **not** restarted until the next relevant commit, so a crashing agent cannot loop.
4. If the checkout is offline, dirty or diverged, the running process is left alone and the problem goes into the state file's `error` field.
5. `setup --pause` (from another terminal) or Ctrl+C stops the process and the supervisor.

Stopping sends Ctrl+Break (Windows) or SIGTERM (Linux), waits `stopTimeoutSeconds` (default 30), then kills the remaining process tree. On Windows the process tree is in a job object, so killing the poller also stops the agent and leaves no unsupervised duplicate. `timeoutSeconds` and `verifyEntry` do not apply in service mode. Because the supervisor holds the installation lock while the agent runs, the engine self-update and `setup` wait until it is stopped. `state/<experiment-id>.json` shows `status` (`running`, `restarting`, `exited`, `failed`, `stopped`), `pid`, `commitSha` and `restarts`.

### Multi-Agent Collaboration Hub

Run `.\git-repo-poller\poller.ps1` and answer:

| Question | Answer |
|---|---|
| Git repository URL | the hub's GitLab URL |
| Branch | `main` |
| Long-running program? | `y` |
| Program to start | `start.ps1` (the default) |

`start.ps1` starts the single agent configured under `%LOCALAPPDATA%\agent-hub\agents\`. Agent settings and credentials stay there, outside the clone.

**One-time step:** `start.ps1` needs a `.venv` inside the poller's clone. The first start therefore fails with "agent-hub is not installed" (`poller.ps1 status` shows it). Create the venv, then stop with Ctrl+C and start again:

```powershell
cd "$env:LOCALAPPDATA\GitRepoPoller\workspaces\multi-agent-collaboration-hub"
py -3.11 -m venv .venv; .\.venv\Scripts\python -m pip install -e .
```

From then on, every code commit to the hub restarts the agent with the new code. Dependency changes in `pyproject.toml` still need `pip install -e .` again. The hub supports **one bridge process per SQLite database**, so do not also run `start.ps1` for the same agent from another checkout. For unattended setup, [`examples/config.hub.example.json`](examples/config.hub.example.json) holds the same settings.

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
