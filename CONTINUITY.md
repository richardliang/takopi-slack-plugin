Goal (incl. success criteria):
- Extract Slack plugin logic from `../takopi`, package it for publishing, and write README instructions to start the plugin in Takopi after distribution.

Constraints/Assumptions:
- Follow workspace instructions in `AGENTS.md`, including Continuity Ledger updates each turn.
- Use ASCII unless existing files require Unicode.
- `../takopi` is expected to contain the current Slack plugin logic (UNCONFIRMED).

Key decisions:
- Use package name `takopi-slack-plugin` with version `0.0.1` and project URL `zkp2p.xyz`.
- Rename Python package to `takopi_slack_plugin` to align with the new distribution name.

State:
- In progress; package prepared, needs commit and push to main.

Done:
- Located Slack transport source and packaging in `../takopi/packages/takopi-transport-slack`.
- Copied Slack transport package files into this workspace.
- Updated README with install/config/start instructions.
- Updated `pyproject.toml` and README to use name `takopi-slack-plugin`, version `0.0.1`, and homepage `https://zkp2p.xyz`.
- Updated entrypoint module path to `takopi_slack_plugin.backend:BACKEND` and renamed package directory.
- Built sdist and wheel via `uv build`.
- Attempted `uv publish`; failed due to missing PyPI credentials/trusted publishing token.
- Added GitHub Actions trusted publishing workflow at `.github/workflows/workflow.yml`.

Now:
- Commit changes and push to `main`.

Next:
- Trigger release (tag or workflow dispatch) after PyPI trusted publishing is configured.

Open questions (UNCONFIRMED if needed):
- Confirm repo/owner/workflow path for trusted publishing and optional environment name (UNCONFIRMED).

Working set (files/ids/commands):
- CONTINUITY.md
- /home/ubuntu/zkp2p/takopi/packages/takopi-transport-slack
- pyproject.toml
- README.md
- src/takopi_slack_plugin
- .github/workflows/workflow.yml
