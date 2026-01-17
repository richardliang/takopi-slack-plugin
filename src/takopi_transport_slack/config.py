from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from takopi.api import ConfigError


@dataclass(frozen=True, slots=True)
class SlackTransportSettings:
    bot_token: str
    channel_id: str
    message_overflow: Literal["trim", "split"] = "trim"
    reply_in_thread: bool = False
    require_mention: bool = False
    poll_interval_s: float = 1.0

    @classmethod
    def from_config(
        cls, config: object, *, config_path: Path
    ) -> "SlackTransportSettings":
        if isinstance(config, SlackTransportSettings):
            return config
        if not isinstance(config, dict):
            raise ConfigError(
                f"Invalid `transports.slack` in {config_path}; expected a table."
            )

        bot_token = _require_str(config, "bot_token", config_path=config_path)
        channel_id = _require_str(config, "channel_id", config_path=config_path)

        message_overflow = config.get("message_overflow", "trim")
        if not isinstance(message_overflow, str):
            raise ConfigError(
                f"Invalid `transports.slack.message_overflow` in {config_path}; "
                "expected a string."
            )
        message_overflow = message_overflow.strip()
        if message_overflow not in {"trim", "split"}:
            raise ConfigError(
                f"Invalid `transports.slack.message_overflow` in {config_path}; "
                "expected 'trim' or 'split'."
            )

        reply_in_thread = _optional_bool(
            config, "reply_in_thread", config_path=config_path, default=False
        )
        require_mention = _optional_bool(
            config, "require_mention", config_path=config_path, default=False
        )
        poll_interval_s = _optional_float(
            config, "poll_interval_s", config_path=config_path, default=1.0
        )

        return cls(
            bot_token=bot_token,
            channel_id=channel_id,
            message_overflow=message_overflow,
            reply_in_thread=reply_in_thread,
            require_mention=require_mention,
            poll_interval_s=poll_interval_s,
        )


def _require_str(config: dict[str, Any], key: str, *, config_path: Path) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            "expected a non-empty string."
        )
    return value.strip()


def _optional_bool(
    config: dict[str, Any],
    key: str,
    *,
    config_path: Path,
    default: bool,
) -> bool:
    if key not in config:
        return default
    value = config.get(key)
    if isinstance(value, bool):
        return value
    raise ConfigError(
        f"Invalid `transports.slack.{key}` in {config_path}; expected a boolean."
    )


def _optional_float(
    config: dict[str, Any],
    key: str,
    *,
    config_path: Path,
    default: float,
) -> float:
    if key not in config:
        return default
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            "expected a number."
        )
    value = float(value)
    if value < 0:
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; must be >= 0."
        )
    return value
