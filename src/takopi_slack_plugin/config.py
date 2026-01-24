from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from takopi.api import ConfigError

DEFAULT_DENY_GLOBS = [
    ".git/**",
    ".env",
    ".envrc",
    "**/*.pem",
    "**/.ssh/**",
]


@dataclass(frozen=True, slots=True)
class SlackFilesSettings:
    enabled: bool = False
    auto_put: bool = True
    auto_put_mode: Literal["upload", "prompt"] = "upload"
    uploads_dir: str = "incoming"
    allowed_user_ids: list[str] = field(default_factory=list)
    deny_globs: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_GLOBS))
    max_upload_bytes: int = 20 * 1024 * 1024
    max_download_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_config(
        cls, config: object, *, config_path: Path
    ) -> "SlackFilesSettings":
        if config is None:
            return cls()
        if isinstance(config, SlackFilesSettings):
            return config
        if not isinstance(config, dict):
            raise ConfigError(
                f"Invalid `transports.slack.files` in {config_path}; "
                "expected a table."
            )

        enabled = _optional_bool(config, "enabled", False, config_path)
        auto_put = _optional_bool(config, "auto_put", True, config_path)
        auto_put_mode = config.get("auto_put_mode", "upload")
        if not isinstance(auto_put_mode, str):
            raise ConfigError(
                f"Invalid `transports.slack.files.auto_put_mode` in {config_path}; "
                "expected a string."
            )
        auto_put_mode = auto_put_mode.strip().lower()
        if auto_put_mode not in {"upload", "prompt"}:
            raise ConfigError(
                f"Invalid `transports.slack.files.auto_put_mode` in {config_path}; "
                "expected 'upload' or 'prompt'."
            )

        uploads_dir = config.get("uploads_dir", "incoming")
        if not isinstance(uploads_dir, str) or not uploads_dir.strip():
            raise ConfigError(
                f"Invalid `transports.slack.files.uploads_dir` in {config_path}; "
                "expected a non-empty string."
            )
        uploads_dir = uploads_dir.strip()
        if Path(uploads_dir).is_absolute():
            raise ConfigError(
                f"Invalid `transports.slack.files.uploads_dir` in {config_path}; "
                "expected a relative path."
            )

        allowed_user_ids = _optional_str_list(
            config,
            "allowed_user_ids",
            [],
            config_path,
            label="transports.slack.files.allowed_user_ids",
        )
        deny_globs = _optional_str_list(
            config,
            "deny_globs",
            DEFAULT_DENY_GLOBS,
            config_path,
            label="transports.slack.files.deny_globs",
        )
        max_upload_bytes = _optional_int(
            config,
            "max_upload_bytes",
            20 * 1024 * 1024,
            config_path,
            label="transports.slack.files.max_upload_bytes",
        )
        max_download_bytes = _optional_int(
            config,
            "max_download_bytes",
            50 * 1024 * 1024,
            config_path,
            label="transports.slack.files.max_download_bytes",
        )

        return cls(
            enabled=enabled,
            auto_put=auto_put,
            auto_put_mode=auto_put_mode,
            uploads_dir=uploads_dir,
            allowed_user_ids=allowed_user_ids,
            deny_globs=deny_globs,
            max_upload_bytes=max_upload_bytes,
            max_download_bytes=max_download_bytes,
        )


@dataclass(frozen=True, slots=True)
class SlackTransportSettings:
    bot_token: str
    channel_id: str
    app_token: str
    message_overflow: Literal["trim", "split"] = "split"
    files: SlackFilesSettings = field(default_factory=SlackFilesSettings)

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

        files = SlackFilesSettings.from_config(
            config.get("files"), config_path=config_path
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


def _optional_str(
    config: dict[str, Any],
    key: str,
    default: str | None,
    config_path: Path,
    *,
    label: str | None = None,
) -> str | None:
    if key not in config:
        return default
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        name = label or f"transports.slack.{key}"
        raise ConfigError(f"Invalid `{name}` in {config_path}; expected a string.")
    cleaned = value.strip()
    return cleaned or None


def _optional_bool(
    config: dict[str, Any],
    key: str,
    default: bool,
    config_path: Path,
    *,
    label: str | None = None,
) -> bool:
    if key not in config:
        return default
    value = config.get(key)
    if isinstance(value, bool):
        return value
    name = label or f"transports.slack.{key}"
    raise ConfigError(f"Invalid `{name}` in {config_path}; expected a boolean.")


def _optional_int(
    config: dict[str, Any],
    key: str,
    default: int,
    config_path: Path,
    *,
    label: str | None = None,
) -> int:
    if key not in config:
        return default
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        name = label or f"transports.slack.{key}"
        raise ConfigError(f"Invalid `{name}` in {config_path}; expected an integer.")
    if value < 0:
        name = label or f"transports.slack.{key}"
        raise ConfigError(f"Invalid `{name}` in {config_path}; must be >= 0.")
    return value


def _optional_str_list(
    config: dict[str, Any],
    key: str,
    default: Sequence[str],
    config_path: Path,
    *,
    label: str | None = None,
) -> list[str]:
    if key not in config:
        return list(default)
    value = config.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        name = label or f"transports.slack.{key}"
        raise ConfigError(f"Invalid `{name}` in {config_path}; expected a list of strings.")
    return [item.strip() for item in value if item.strip()]
