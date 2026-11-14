# Agent development instructions

This project will become a **small Windows PowerShell 7 repo poller**. It is not a new autonomous AI framework. Read [README.md](README.md) and [the source extraction issue](https://github.com/tire0011/ai-native-code-bench/issues/3) before editing.

## Scope

- Implement a tiny installed bootstrapper: local lock, Git fetch + safe fast-forward of approved clean checkouts, fixed configured command invocation, timeout, local structured status/logs.
- Each experiment repository owns its own `run.ps1` (optionally `verify.ps1`). Two experiments must run independently from one poller installation.
- Installer: a single interactive `setup.ps1` for Windows PowerShell 7 (current user, no admin by default), plus documented unattended, dry-run, repair, pause/resume, and uninstall modes.
- Own updater: keep the poller outside experiment checkouts; update only between runs, with last-known-good fallback; no hot self-modification.
- A future mailbox adapter may request only locally approved experiment IDs. Do not add an untrusted remote-command interface.

## Non-negotiable checks

- No `git reset --hard`, forced push, automatic destructive cleanup, or overwriting dirty worktrees.
- No secrets, corporate files, raw model outputs or private logs in commits or public results. Gitignore is defense-in-depth, not authorization for publishing.
- Do not enable scheduled tasks, expensive inference, auto-push or new background services without explicit opt-in.
- Treat `exit 0` as execution success only; validity requires a separate verification result.
- Prefer standard PowerShell 7 and Git over new dependencies, heavyweight frameworks or background daemons.
- Use local temporary Git remotes and dummy commands for tests. Cover initial clone, updates, unchanged revisions, dirty/diverged states, failures, timeouts, concurrency, offline resume, and two configurations.
- Do not claim any feature is implemented, tested or deployed until independently verified.

## Delivery

Open a PR in this repo with tests, command examples and clear migration notes for the existing [AI Native Code Bench](https://github.com/tire0011/ai-native-code-bench). Keep the original benchmark working until replacement has passed comparable tests. Do not merge automatically.
