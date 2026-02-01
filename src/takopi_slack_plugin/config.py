from __future__ import annotations

import json
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


@dataclass(frozen=True, slots=True)
class SlackActionHandler:
    action_id: str
    command: str
    args: str = ""


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
    action_handlers: list[SlackActionHandler] = field(default_factory=list)
    action_blocks: list[dict[str, Any]] | None = None
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
        if "action_buttons" in config:
            raise ConfigError(
                f"Invalid `transports.slack.action_buttons` in {config_path}; "
                "action_buttons is no longer supported. Use action_handlers + "
                "action_blocks instead."
            )
        if "show_running" in config:
            raise ConfigError(
                f"Invalid `transports.slack.show_running` in {config_path}; "
                "show_running is no longer supported."
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

        action_handlers = _optional_action_handlers(
            config,
            "action_handlers",
            config_path,
        )
        action_blocks = _optional_action_blocks(
            config,
            "action_blocks",
            config_path,
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
            action_handlers=action_handlers,
            action_blocks=action_blocks,
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


def _optional_action_handlers(
    config: dict[str, Any],
    key: str,
    config_path: Path,
) -> list[SlackActionHandler]:
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
    handlers: list[SlackActionHandler] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}]` in {config_path}; "
                "expected a table."
            )
        allowed = {"action_id", "id", "command", "args"}
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

        action_id_value = raw.get("action_id")
        if action_id_value is None:
            action_id_value = raw.get("id")
            if not isinstance(action_id_value, str) or not action_id_value.strip():
                raise ConfigError(
                    f"Invalid `transports.slack.{key}[{idx}].action_id` in {config_path}; "
                    "expected a non-empty string."
                )
            action_id = _slugify_action_id(action_id_value)
            action_id = f"takopi-slack:action:{action_id}"
        else:
            if not isinstance(action_id_value, str) or not action_id_value.strip():
                raise ConfigError(
                    f"Invalid `transports.slack.{key}[{idx}].action_id` in {config_path}; "
                    "expected a non-empty string."
                )
            action_id = action_id_value.strip()

        if action_id in seen_ids:
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].action_id` in {config_path}; "
                "duplicate action_id."
            )
        seen_ids.add(action_id)

        args = raw.get("args", "")
        if not isinstance(args, str):
            raise ConfigError(
                f"Invalid `transports.slack.{key}[{idx}].args` in {config_path}; "
                "expected a string."
            )
        args = args.strip()

        handlers.append(
            SlackActionHandler(
                action_id=action_id,
                command=command,
                args=args,
            )
        )

    return handlers


def _optional_action_blocks(
    config: dict[str, Any],
    key: str,
    config_path: Path,
) -> list[dict[str, Any]] | None:
    if key not in config:
        return None
    raw = config.get(key)
    if raw is None:
        return None
    label = f"transports.slack.{key}"
    if isinstance(raw, list) or isinstance(raw, dict):
        return _coerce_block_list(raw, label, config_path)
    if not isinstance(raw, str):
        raise ConfigError(f"Invalid `{label}` in {config_path}; expected JSON or a list.")
    text = raw.strip()
    if not text:
        return None
    if text.startswith("@"):
        path = Path(text[1:]).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"Invalid `{label}` in {config_path}; could not read {path}: {exc}."
            ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid `{label}` in {config_path}; expected valid JSON."
        ) from exc
    return _coerce_block_list(parsed, label, config_path)


def _coerce_block_list(
    value: Any,
    label: str,
    config_path: Path,
) -> list[dict[str, Any]]:
    blocks = value
    if isinstance(blocks, dict):
        blocks = blocks.get("blocks")
    if not isinstance(blocks, list) or not all(
        isinstance(item, dict) for item in blocks
    ):
        raise ConfigError(
            f"Invalid `{label}` in {config_path}; expected a list of block objects."
        )
    return list(blocks)


def _slugify_action_id(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"[^a-z0-9_-]", "", cleaned)
    cleaned = cleaned.strip("-_")
    if not cleaned or not _ACTION_ID_RE.match(cleaned):
        raise ConfigError(
            "Invalid `transports.slack.action_handlers.id` value; "
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
