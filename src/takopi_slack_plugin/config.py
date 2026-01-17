from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from takopi.api import ConfigError


@dataclass(frozen=True, slots=True)
class SlackTransportSettings:
    bot_token: str
    channel_id: str
    app_token: str
    message_overflow: Literal["trim", "split"] = "trim"
    reply_in_thread: bool = False
    require_mention: bool = False
    session_mode: Literal["stateless", "thread"] = "stateless"

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
        if "poll_interval_s" in config:
            raise ConfigError(
                f"Invalid `transports.slack.poll_interval_s` in {config_path}; "
                "polling mode has been removed."
            )
        socket_mode = config.get("socket_mode")
        if socket_mode is not None and socket_mode is not True:
            raise ConfigError(
                f"Invalid `transports.slack.socket_mode` in {config_path}; "
                "socket mode is required."
            )
        app_token = _require_str(config, "app_token", config_path=config_path)

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
        session_mode = config.get("session_mode", "stateless")
        if not isinstance(session_mode, str):
            raise ConfigError(
                f"Invalid `transports.slack.session_mode` in {config_path}; "
                "expected a string."
            )
        session_mode = session_mode.strip()
        if session_mode not in {"stateless", "thread"}:
            raise ConfigError(
                f"Invalid `transports.slack.session_mode` in {config_path}; "
                "expected 'stateless' or 'thread'."
            )

        return cls(
            bot_token=bot_token,
            channel_id=channel_id,
            app_token=app_token,
            message_overflow=message_overflow,
            reply_in_thread=reply_in_thread,
            require_mention=require_mention,
            session_mode=session_mode,
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

