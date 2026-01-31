from __future__ import annotations

import re
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

_ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_MAX_CUSTOM_ACTIONS = 4


@dataclass(frozen=True, slots=True)
class SlackActionButton:
    id: str
    label: str
    command: str
    args: str = ""
    style: Literal["primary", "danger"] | None = None

    @property
    def action_id(self) -> str:
        return f"takopi-slack:action:{self.id}"


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
    action_buttons: list[SlackActionButton] = field(default_factory=list)
    github_user_tokens: dict[str, str] = field(default_factory=dict)
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

        action_buttons = _optional_action_buttons(
            config,
            "action_buttons",
            config_path,
        )

        github_user_tokens = _optional_str_dict(
            config,
            "github_user_tokens",
            {},
            config_path,
            label="transports.slack.github_user_tokens",
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
            action_buttons=action_buttons,
            github_user_tokens=github_user_tokens,
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


def _optional_str_dict(
    config: dict[str, Any],
    key: str,
    default: dict[str, str],
    config_path: Path,
    *,
    label: str | None = None,
) -> dict[str, str]:
    if key not in config:
        return dict(default)
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        name = label or f"transports.slack.{key}"
        raise ConfigError(f"Invalid `{name}` in {config_path}; expected a table.")
    cleaned: dict[str, str] = {}
    name = label or f"transports.slack.{key}"
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ConfigError(
                f"Invalid `{name}` in {config_path}; expected a table of strings."
            )
        user_id = raw_key.strip()
        token = raw_value.strip()
        if not user_id or not token:
            raise ConfigError(
                f"Invalid `{name}` in {config_path}; expected non-empty strings."
            )
        cleaned[user_id] = token
    return cleaned


def _optional_action_buttons(
    config: dict[str, Any],
    key: str,
    config_path: Path,
) -> list[SlackActionButton]:
    if key not in config:
        return []
    value = config.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            "expected a list of tables."
        )

    buttons: list[SlackActionButton] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}]` in {config_path}; "
                "expected a table."
            )
        allowed = {"id", "label", "command", "args", "style"}
        unknown_keys = set(raw) - allowed
        if unknown_keys:
            unknown = ", ".join(sorted(unknown_keys))
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}]` in {config_path}; "
                f"unknown keys: {unknown}."
            )

        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].command` in {config_path}; "
                "expected a non-empty string."
            )
        command = command.strip().lstrip("/").lower()
        for prefix in ("takopi-", "takopi_"):
            if command.startswith(prefix) and len(command) > len(prefix):
                command = command[len(prefix) :]
                break

        label = raw.get("label")
        if label is None:
            label = command
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].label` in {config_path}; "
                "expected a non-empty string."
            )
        label = label.strip()

        button_id = raw.get("id")
        if button_id is None:
            button_id = label
        if not isinstance(button_id, str) or not button_id.strip():
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].id` in {config_path}; "
                "expected a non-empty string."
            )
        button_id = _slugify_action_id(button_id)
        if button_id in seen_ids:
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].id` in {config_path}; "
                "duplicate id."
            )
        seen_ids.add(button_id)

        args = raw.get("args", "")
        if not isinstance(args, str):
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].args` in {config_path}; "
                "expected a string."
            )
        args = args.strip()

        style = raw.get("style")
        if style is not None:
            if not isinstance(style, str):
                raise ConfigError(
                    f"Invalid `transports.slack.{key}[{idx}].style` in {config_path}; "
                    "expected a string."
                )
            style = style.strip().lower()
            if style not in {"primary", "danger"}:
                raise ConfigError(
                    f"Invalid `transports.slack.{key}[{idx}].style` in {config_path}; "
                    "expected 'primary' or 'danger'."
                )

        buttons.append(
            SlackActionButton(
                id=button_id,
                label=label,
                command=command,
                args=args,
                style=style,
            )
        )

    if len(buttons) > _MAX_CUSTOM_ACTIONS:
        raise ConfigError(
            f"Invalid `transports.slack.{key}` in {config_path}; "
            f"expected at most {_MAX_CUSTOM_ACTIONS} buttons."
        )

    return buttons


def _slugify_action_id(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"[^a-z0-9_-]", "", cleaned)
    cleaned = cleaned.strip("-_")
    if not cleaned or not _ACTION_ID_RE.match(cleaned):
        raise ConfigError(
            "Invalid `transports.slack.action_buttons.id` value; "
            "expected 1-63 chars of [a-z0-9_-], starting with a letter or digit."
        )
    return cleaned


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
