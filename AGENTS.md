# Agent development instructions

This project is a **small Git repo poller**, implemented in Python 3.10+ (standard library only) with `poller.ps1` as the only PowerShell file (a wrapper). It is not a new autonomous AI framework. Read [README.md](README.md) and [the source extraction issue](https://github.com/tire0011/ai-native-code-bench/issues/3) before editing.

## Scope

- Implement a tiny installed bootstrapper: local lock, Git fetch + safe fast-forward of approved clean checkouts, fixed configured command invocation, timeout, local structured status/logs.
- **One experiment per instance**: each installation's config has a single `experiment` object. Multiple experiments run as multiple independent poller installations, never as threads in one process. Each experiment repository owns its own entrypoint (`run.py` or `run.ps1`, optionally a verify script).
- Installer: `poller.ps1 setup` (interactive, current user, no admin by default), plus unattended, dry-run, repair, pause/resume, and uninstall modes.
- Own updater: keep the poller outside experiment checkouts; update only between runs, with last-known-good fallback; no hot self-modification.
- A future mailbox adapter may request only locally approved experiment IDs. Do not add an untrusted remote-command interface.

## Non-negotiable checks

- No `git reset --hard`, forced push, automatic destructive cleanup, or overwriting dirty worktrees.
- No secrets, corporate files, raw model outputs or private logs in commits or public results. Gitignore is defense-in-depth, not authorization for publishing.
- Do not enable scheduled tasks, expensive inference, auto-push or new background services without explicit opt-in.
- Treat `exit 0` as execution success only; validity requires a separate verification result.
- Prefer the Python standard library and Git over new dependencies, heavyweight frameworks or background daemons. Keep PowerShell limited to the wrapper.
- Use local temporary Git remotes and dummy commands for tests. Cover initial clone, updates, unchanged revisions, dirty/diverged states, failures, timeouts, locks/busy ticks, offline resume, result publishing, interruption, two independent installations, legacy config migration and experiment replacement.
- Do not claim any feature is implemented, tested or deployed until independently verified.

## Delivery

For now changes are committed directly to `main` (owner decision); this may switch back to pull requests later. Run `python -m unittest discover -s tests -t .` before every push and include test commands and migration notes for the [AI Native Code Bench](https://github.com/tire0011/ai-native-code-bench). Keep the original benchmark working until the replacement has passed comparable tests.
