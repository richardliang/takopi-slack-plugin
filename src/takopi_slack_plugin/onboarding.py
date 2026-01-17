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

    bot_token = questionary.password("Slack bot token").ask()
    if not bot_token:
        return False
    app_token = questionary.password("Slack app token (xapp-)").ask()
    if not app_token:
        return False
    channel_id = questionary.text("Slack channel ID").ask()
    if not channel_id:
        return False
    transports = _ensure_table(config, "transports", config_path=config_path)
    slack = _ensure_table(
        transports,
        "slack",
        config_path=config_path,
        label="transports.slack",
    )
    slack["bot_token"] = str(bot_token).strip()
    slack["app_token"] = str(app_token).strip()
    slack["channel_id"] = str(channel_id).strip()
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
