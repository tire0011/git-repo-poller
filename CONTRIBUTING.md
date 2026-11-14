# Contributing

Thanks for your interest in Git Repo Poller.

This project is in the initial implementation stage. Please discuss significant behavior changes in an issue and submit a pull request referencing it.

## Development guidelines

- Target **Windows with PowerShell 7**; avoid elevated privileges by default.
- Keep the poller small and independent from any experiment, LLM provider or mailbox transport.
- Add automated tests using temporary local Git repositories and inexpensive dummy entrypoints. No cloud services or GPU should be needed to run the normal test suite.
- Include a dry-run path for installer and update operations. Preserve the user's existing files and Git changes.
- Do not commit credentials, local configurations, private workspace contents, unredacted command output, or company data.
- Document any prerequisite, expected command, behavior change and rollback procedure.
- Include test commands and observed results in your PR. Only claim successful behavior after testing.

See [AGENTS.md](AGENTS.md) for coding-agent constraints and [README.md](README.md) for the current project scope.

Contributions are intended to be licensed under the project's [MIT License](LICENSE); confirm that you have rights to any code you contribute and retain required third-party notices.
