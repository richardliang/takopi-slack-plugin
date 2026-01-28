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

        allowed_keys = {
            "enabled",
            "auto_put",
            "auto_put_mode",
            "uploads_dir",
            "allowed_user_ids",
            "deny_globs",
        }
        unknown_keys = set(config) - allowed_keys
        if unknown_keys:
            unknown = ", ".join(sorted(unknown_keys))
            raise ConfigError(
                f"Invalid `transports.slack.files` in {config_path}; "
                f"unknown keys: {unknown}."
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
        return cls(
            enabled=enabled,
            auto_put=auto_put,
            auto_put_mode=auto_put_mode,
            uploads_dir=uploads_dir,
            allowed_user_ids=allowed_user_ids,
            deny_globs=deny_globs,
        )


@dataclass(frozen=True, slots=True)
class SlackTransportSettings:
    bot_token: str
    channel_id: str
    app_token: str
    message_overflow: Literal["trim", "split"] = "split"
    files: SlackFilesSettings = field(default_factory=SlackFilesSettings)
    stale_worktree_reminder: bool = False
    stale_worktree_hours: float = 24.0
    stale_worktree_check_interval_s: float = 600.0

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

        stale_worktree_reminder = config.get("stale_worktree_reminder", False)
        if not isinstance(stale_worktree_reminder, bool):
            raise ConfigError(
                f"Invalid `transports.slack.stale_worktree_reminder` in {config_path}; "
                "expected true or false."
            )

        stale_worktree_hours = _require_number(
            config,
            "stale_worktree_hours",
            default=24.0,
            config_path=config_path,
            min_value=0.5,
        )
        stale_worktree_check_interval_s = _require_number(
            config,
            "stale_worktree_check_interval_s",
            default=600.0,
            config_path=config_path,
            min_value=30.0,
        )

        return cls(
            bot_token=bot_token,
            channel_id=channel_id,
            app_token=app_token,
            message_overflow=message_overflow,
            files=files,
            stale_worktree_reminder=stale_worktree_reminder,
            stale_worktree_hours=stale_worktree_hours,
            stale_worktree_check_interval_s=stale_worktree_check_interval_s,
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


def _require_number(
    config: dict[str, Any],
    key: str,
    *,
    default: float,
    config_path: Path,
    min_value: float | None = None,
) -> float:
    value = config.get(key, default)
    if not isinstance(value, (int, float)):
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            "expected a number."
        )
    value = float(value)
    if min_value is not None and value < min_value:
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            f"expected >= {min_value}."
        )
    return value
