Goal (incl. success criteria):
- Merge PR #1 and publish version `0.1.1`.

Constraints/Assumptions:
- Follow workspace instructions in `AGENTS.md`, including Continuity Ledger updates each turn.
- Use ASCII unless existing files require Unicode.
- `../takopi` is expected to contain the current Slack plugin logic (UNCONFIRMED).
- Gating policy should live in global `~/.codex/AGENTS.md` and docs, not enforced in code.

Key decisions:
- Use package name `takopi-slack-plugin` and project URL `zkp2p.xyz`; bump to `0.0.4` for Socket Mode bugfix release.
- Rename Python package to `takopi_slack_plugin` to align with the new distribution name.

State:
- Version `0.1.1` committed and tag `v0.1.1` pushed to trigger publish.

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
- Simplified Slack config to minimal required fields and fixed app_token ordering error.
- Bumped version to `0.0.10`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.10`.
- Removed leftover legacy Slack config reads (poll_interval/socket_mode).
- Refactored Slack thread sessions to use JsonStateStore and store resume tokens only.
- Hardcoded thread replies and removed mention/session config flags.
- Enforced directives using single parse and updated socket naming/docs.
- Bumped version to `0.0.11`, committed, and pushed to `main`.
- Created and pushed tag `v0.0.11`.
- Cloned `takopi-discord` to `/tmp/takopi-discord` and reviewed `README.md`, `pyproject.toml`, and `src/takopi_discord/state.py`.
- Reviewed Discord transport core files for features and patterns (`loop.py`, `handlers.py`, `bridge.py`, `commands/registration.py`, `overrides.py`, `outbox.py`, `file_transfer.py`).
- Added Slack thread state storage (context, resume tokens, model/reasoning overrides, default engine) and helpers.
- Added shared engine runner helper and Slack outbox for rate-limited send/edit/delete.
- Added Slack slash command/shortcut routing for plugin commands plus built-in override controls.
- Added cancel button blocks + interactive handler and enabled message splitting by default.
- Updated README for slash command usage, shortcuts, cancel button, and message_overflow.
- Bumped version to `0.0.12`, committed, pushed to `main`, and tagged `v0.0.12`.
- Updated Slack thread handling to reuse stored context for replies without directives.
- Bumped version to `0.0.13`, committed, pushed to `main`, and tagged `v0.0.13`.
- Refactored Slack bridge to reuse helper functions for thread context/overrides and align keyword-only handler signatures.
- Rewrote README to match takopi main style and added license section (MIT already present).
- Bumped version to `0.0.14`, committed docs/refactor changes, pushed to `main`, and tagged `v0.0.14`.
- Fixed anyio TaskGroup.start_soon kwargs crash; bumped to `0.0.15`, committed, pushed to `main`, and tagged `v0.0.15`.
- Bumped version to `0.1.0`, committed, pushed to `main`, and tagged `v0.1.0`.
- Added repo `AGENTS.md` with worktree sync policy (pending removal per user).
- Added worktree sync policy to `/home/ubuntu/.codex/AGENTS.md`.
- Removed repo `AGENTS.md`.
- Updated README with optional directives and AGENTS customization note.
- Removed read-only gating/prefix in Slack bridge and allowed runs without directives.
- Updated README to describe optional directives and link to gating docs.
- Added `AGENTS.example.md` and `GATING_README.md`.
- Removed gating policy from `/home/ubuntu/.codex/AGENTS.md`.
- Merged opinionated gating into `/home/ubuntu/.codex/AGENTS.md`.
- Moved gating docs into `docs/` and updated README/doc links.
- Added ignore-worktrees instruction to global AGENTS and example.
- Pushed commit updating `docs/AGENTS.example.md` to PR branch.
- Merged PR #1 via main fast-forward update.
- Bumped version to `0.1.1`, committed, and pushed tag `v0.1.1`.

Now:
- Wait for publish workflow to complete.

Next:
- Confirm release is live.

Open questions (UNCONFIRMED if needed):
- None.

Working set (files/ids/commands):
- CONTINUITY.md
- /home/ubuntu/zkp2p/takopi/readme.md
- README.md
- LICENSE
- pyproject.toml
- src/takopi_slack_plugin/bridge.py
- src/takopi_slack_plugin/client.py
- src/takopi_slack_plugin/config.py
- /tmp/takopi-discord
- /home/ubuntu/.codex/AGENTS.md
- docs/AGENTS.example.md
- docs/GATING_README.md
- pyproject.toml
