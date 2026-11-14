# Git Repo Poller

A **minimal Windows PowerShell 7 Git poller** for running local, explicitly configured experiment commands when a trusted repository changes.

**Status: design / bootstrap phase — not yet implemented.** This repository currently contains project metadata and the implementation specification, not a working poller or installer.

## What it will do

1. Poll an approved Git repository and fetch updates.
2. Safely fast-forward a clean experiment checkout, without discarding local work.
3. Optionally execute one configured local entry point (for example, `run.ps1`) when the relevant revision changes.
4. Record run status and logs locally; optionally use a separate, explicit results publisher.

Each experiment owns its commands, tests, dependencies and model configuration. The poller itself stays small, requires **no GPU or AI model**, and lives outside the experiment checkouts. Its own updates take effect *between* runs.

```text
Windows Task Scheduler
        |
        v
git-repo-poller (stable install)
        +----> approved experiment A repo ---> run.ps1
        +----> approved experiment B repo ---> run.ps1
        |
        '---> local run status / logs
```

## Setup — planned

The intended Windows installation is one interactive wizard:

```powershell
pwsh -File .\setup.ps1
```

It will ask for a worker ID, approved repository URLs, folders, Git polling interval and optional **current-user** Task Scheduler integration. It should validate prerequisites, support non-interactive automation, test connectivity, and offer repair, pause/resume and uninstall without deleting user results by default.

**Important:** `setup.ps1` does **not exist yet**. Please follow the implementation issue before attempting installation. Do not run arbitrary install scripts copied from third-party comments.

## Separation of responsibilities

- **Poller:** Git update + configured entry-point invocation + local status.
- **Experiment repository:** `run.ps1`, optional `verify.ps1`, models and experiment-specific logic.
- **Mailbox (later integration):** addressed task delivery and replies, not execution logic; see [agent-lab-mailbox#7](https://github.com/tire0011/agent-lab-mailbox/issues/7).
- **Corporate use:** use a company-authorized internal remote and isolated credentials; never publish proprietary code, prompts or logs to this public repository.

## Development and origin

The extraction originates from [ai-native-code-bench issue #3](https://github.com/tire0011/ai-native-code-bench/issues/3), including its existing `watch.ps1`, `watch-loop.ps1`, and `sync-results.ps1` behavior.

Implementation belongs in this standalone repository, with source benchmark backward compatibility and verified tests. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) before submitting changes.

## License

[MIT License](LICENSE) © 2026 Rene Tittel. When importing code from other projects, review its contributor rights and third-party license notices before reuse.
