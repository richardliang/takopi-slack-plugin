from __future__ import annotations

from pathlib import Path
from typing import Any

import questionary

from takopi.api import ConfigError, HOME_CONFIG_PATH, read_config, write_config


async def interactive_setup(*, force: bool) -> bool:
    _ = force
    config_path = HOME_CONFIG_PATH
    try:
        config = read_config(config_path)
    except ConfigError:
        config = {}

    token = questionary.password("Slack bot token").ask()
    if not token:
        return False
    channel_id = questionary.text("Slack channel ID").ask()
    if not channel_id:
        return False
    reply_in_thread = questionary.confirm(
        "Reply in thread?",
        default=False,
    ).ask()
    require_mention = questionary.confirm(
        "Require @bot mention?",
        default=False,
    ).ask()
    session_mode = questionary.confirm(
        "Remember sessions per thread?",
        default=False,
    ).ask()

    transports = _ensure_table(config, "transports", config_path=config_path)
    slack = _ensure_table(
        transports,
        "slack",
        config_path=config_path,
        label="transports.slack",
    )
    slack["bot_token"] = str(token).strip()
    slack["channel_id"] = str(channel_id).strip()
    slack["reply_in_thread"] = bool(reply_in_thread)
    slack["require_mention"] = bool(require_mention)
    if session_mode:
        slack["session_mode"] = "thread"
    config["transport"] = "slack"
    write_config(config, config_path)
    return True


def _ensure_table(
    config: dict[str, Any],
    key: str,
    *,
    config_path: Path,
    label: str | None = None,
) -> dict[str, Any]:
    value = config.get(key)
    if value is None:
        table: dict[str, Any] = {}
        config[key] = table
        return table
    if not isinstance(value, dict):
        name = label or key
        raise ConfigError(f"Invalid `{name}` in {config_path}; expected a table.")
    return value
