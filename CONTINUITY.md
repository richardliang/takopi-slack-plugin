Goal (incl. success criteria):
- Require Socket Mode only (remove polling) and clean up unused Slack transport code.

Constraints/Assumptions:
- Follow workspace instructions in `AGENTS.md`, including Continuity Ledger updates each turn.
- Use ASCII unless existing files require Unicode.
- `../takopi` is expected to contain the current Slack plugin logic (UNCONFIRMED).

Key decisions:
- Use package name `takopi-slack-plugin` and project URL `zkp2p.xyz`; bump to `0.0.4` for Socket Mode bugfix release.
- Rename Python package to `takopi_slack_plugin` to align with the new distribution name.

State:
- In progress; Slack context shortcut changes reverted on `main`; tag `v0.0.5` still exists on remote; `v0.0.6`, `v0.0.7`, `v0.0.8`, and `v0.0.9` tagged and pushed.

Done:
- Located Slack transport source and packaging in `../takopi/packages/takopi-transport-slack`.
- Copied Slack transport package files into this workspace.
- Updated README with install/config/start instructions.
- Updated `pyproject.toml` and README to use name `takopi-slack-plugin`, version `0.0.1`, and homepage `https://zkp2p.xyz`.
- Updated entrypoint module path to `takopi_slack_plugin.backend:BACKEND` and renamed package directory.
- Built sdist and wheel via `uv build`.
- Attempted `uv publish`; failed due to missing PyPI credentials/trusted publishing token.
- Added GitHub Actions trusted publishing workflow at `.github/workflows/workflow.yml`.
- Committed changes and pushed to `main` (remote reports repository moved to `github.com:richardliang/takopi-slack-plugin.git`).
- Created and pushed tag `v0.0.1` to trigger publish workflow.
- Added Socket Mode support (app token + WebSocket) and kept polling as fallback.
- Updated README/config for Socket Mode and added `websockets` dependency.
- Rebuilt package with `UV_CACHE_DIR=/tmp/uv-cache` after cache permission error.
- Bumped version to `0.0.2`, committed, and pushed Socket Mode changes to `main`.
- Created and pushed tag `v0.0.2`.
- Fixed TaskGroup `start_soon` usage in Socket Mode/polling loops.
- Bumped version to `0.0.3`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.3`.
- Fixed Socket Mode handler signature; bumped to `0.0.4`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.4`.
- Added Slack context store and directive-only context setting; updated README (reverting).
- Bumped version to `0.0.5`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.5`.
- Reverted Slack context shortcut changes; `main` version back to `0.0.4`.
- Bumped version to `0.0.6`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.6`.
- Added Slack thread session store (resume + context) and wired config/bridge/back-end.
- Updated Slack onboarding/README for thread sessions.
- Bumped version to `0.0.7`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.7`.
- Enforced `/project` + `@branch` directives for Slack messages; updated README.
- Bumped version to `0.0.8`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.8`.
- Removed Slack polling code and required Socket Mode.
- Updated config/onboarding/README for Socket Mode-only setup.
- Bumped version to `0.0.9`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.9`.

Now:
- Share Socket Mode-only requirement and updated config guidance.

Next:
- Confirm any further Slack UX changes (slash commands/app home) if needed.

Open questions (UNCONFIRMED if needed):
- None.

Working set (files/ids/commands):
- CONTINUITY.md
- /home/ubuntu/zkp2p/takopi/packages/takopi-transport-slack
- pyproject.toml
- README.md
- src/takopi_slack_plugin
- .github/workflows/workflow.yml
- src/takopi_slack_plugin/bridge.py
- src/takopi_slack_plugin/client.py
- src/takopi_slack_plugin/config.py
