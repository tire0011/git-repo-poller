# Security policy

Git Repo Poller is early-stage software that will update local working copies and invoke locally approved commands. Treat it as **experimental**, not a hardened service.

## Reporting a vulnerability

Please avoid posting exploits, credentials or sensitive system details in public issues. If the repository's **Private vulnerability reporting** feature is enabled, use its private reporting form. Otherwise contact the repository owner through a private channel before public disclosure.

## Security model to preserve

- Trust and allowlist repository remotes and local entrypoints; do not execute shell text supplied by untrusted messages or remote task bodies.
- Validate remote/revision identity before updating; reject dirty/diverged trees rather than silently overwriting user work.
- Never copy local credentials, environment contents, proprietary source, model prompts or unredacted logs into a public repository.
- Keep mailbox authorization and multi-host message claiming in the mailbox component, not this poller.
- Keep all scheduled tasks, publishing and destructive operations explicit and opt-in.
- Run under normal user privileges by default.

At this stage no stable release or formal security support window is promised.
