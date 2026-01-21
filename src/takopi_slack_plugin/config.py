from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from takopi.api import ConfigError

DEFAULT_FILE_DENY_GLOBS = (
    ".git/**",
    ".env",
    ".envrc",
    "**/*.pem",
    "**/.ssh/**",
)


@dataclass(frozen=True, slots=True)
class SlackFileTransferSettings:
    enabled: bool = False
    auto_put: bool = True
    auto_put_mode: Literal["upload", "prompt"] = "upload"
    uploads_dir: str = "incoming"
    allowed_user_ids: tuple[str, ...] = ()
    deny_globs: tuple[str, ...] = DEFAULT_FILE_DENY_GLOBS

    @classmethod
    def from_config(
        cls, config: object | None, *, config_path: Path
    ) -> "SlackFileTransferSettings":
        if config is None:
            return cls()
        if isinstance(config, SlackFileTransferSettings):
            return config
        if not isinstance(config, dict):
            raise ConfigError(
                f"Invalid `transports.slack.files` in {config_path}; expected a table."
            )

        enabled = _get_bool(
            config,
            "enabled",
            default=False,
            config_path=config_path,
            label="transports.slack.files.enabled",
        )
        auto_put = _get_bool(
            config,
            "auto_put",
            default=True,
            config_path=config_path,
            label="transports.slack.files.auto_put",
        )
        auto_put_mode = config.get("auto_put_mode", "upload")
        if not isinstance(auto_put_mode, str):
            raise ConfigError(
                f"Invalid `transports.slack.files.auto_put_mode` in {config_path}; "
                "expected a string."
            )
        auto_put_mode = auto_put_mode.strip()
        if auto_put_mode not in {"upload", "prompt"}:
            raise ConfigError(
                f"Invalid `transports.slack.files.auto_put_mode` in {config_path}; "
                "expected 'upload' or 'prompt'."
            )
        uploads_dir = _get_str(
            config,
            "uploads_dir",
            default="incoming",
            config_path=config_path,
            label="transports.slack.files.uploads_dir",
        )
        allowed_user_ids = _get_str_list(
            config,
            "allowed_user_ids",
            default=(),
            config_path=config_path,
            label="transports.slack.files.allowed_user_ids",
        )
        deny_globs = _get_str_list(
            config,
            "deny_globs",
            default=DEFAULT_FILE_DENY_GLOBS,
            config_path=config_path,
            label="transports.slack.files.deny_globs",
        )

        return cls(
            enabled=enabled,
            auto_put=auto_put,
            auto_put_mode=auto_put_mode,
            uploads_dir=uploads_dir,
            allowed_user_ids=tuple(allowed_user_ids),
            deny_globs=tuple(deny_globs),
        )


@dataclass(frozen=True, slots=True)
class SlackTransportSettings:
    bot_token: str
    channel_id: str
    app_token: str
    message_overflow: Literal["trim", "split"] = "split"
    files: SlackFileTransferSettings = SlackFileTransferSettings()

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
        app_token = _require_str(config, "app_token", config_path=config_path)

        message_overflow = config.get("message_overflow", "split")
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

        files = SlackFileTransferSettings.from_config(
            config.get("files"),
            config_path=config_path,
        )

        return cls(
            bot_token=bot_token,
            channel_id=channel_id,
            app_token=app_token,
            message_overflow=message_overflow,
            files=files,
        )


def _require_str(config: dict[str, Any], key: str, *, config_path: Path) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            "expected a non-empty string."
        )
    return value.strip()


def _get_bool(
    config: dict[str, Any],
    key: str,
    *,
    default: bool,
    config_path: Path,
    label: str,
) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Invalid `{label}` in {config_path}; expected a bool.")
    return value


def _get_str(
    config: dict[str, Any],
    key: str,
    *,
    default: str,
    config_path: Path,
    label: str,
) -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"Invalid `{label}` in {config_path}; expected a string.")
    trimmed = value.strip()
    if not trimmed:
        raise ConfigError(f"Invalid `{label}` in {config_path}; expected a string.")
    return trimmed


def _get_str_list(
    config: dict[str, Any],
    key: str,
    *,
    default: Iterable[str],
    config_path: Path,
    label: str,
) -> list[str]:
    value = config.get(key, list(default))
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Invalid `{label}` in {config_path}; expected a list.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(
                f"Invalid `{label}` in {config_path}; expected strings."
            )
        trimmed = item.strip()
        if not trimmed:
            raise ConfigError(
                f"Invalid `{label}` in {config_path}; expected non-empty strings."
            )
        result.append(trimmed)
    return result
