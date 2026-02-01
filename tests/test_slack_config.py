from pathlib import Path

import pytest

from takopi.api import ConfigError
from takopi_slack_plugin.config import SlackTransportSettings


def test_from_config_valid() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "message_overflow": "split",
        "action_handlers": [
            {
                "action_id": "takopi-slack:action:deploy",
                "command": "preview",
                "args": "start",
            }
        ],
        "action_blocks": '[{"type":"section","text":{"type":"mrkdwn","text":"hi"}}]',
        "files": {
            "enabled": True,
            "auto_put": False,
            "auto_put_mode": "prompt",
            "uploads_dir": "incoming",
            "allowed_user_ids": ["U123"],
        },
    }
    settings = SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))
    assert settings.bot_token == "xoxb-1"
    assert settings.channel_id == "C123"
    assert settings.app_token == "xapp-1"
    assert settings.message_overflow == "split"
    assert settings.files.enabled is True
    assert settings.files.auto_put is False
    assert settings.files.auto_put_mode == "prompt"
    assert settings.files.allowed_user_ids == ["U123"]
    assert settings.action_handlers[0].action_id == "takopi-slack:action:deploy"
    assert settings.action_handlers[0].command == "preview"
    assert settings.action_blocks == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}
    ]


def test_from_config_missing_key() -> None:
    cfg = {"bot_token": "xoxb-1", "channel_id": "C123"}
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_invalid_table() -> None:
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config("nope", config_path=Path("/tmp/x"))


def test_from_config_invalid_message_overflow() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "message_overflow": "bad",
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_invalid_files_table() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "files": "nope",
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_rejects_action_buttons() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "action_buttons": [{"id": "preview", "command": "preview"}],
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_rejects_show_running() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "show_running": False,
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_invalid_uploads_dir() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "files": {"uploads_dir": "/abs/path"},
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_unknown_files_key() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "files": {"max_upload_bytes": 1024},
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_duplicate_action_handlers() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "action_handlers": [
            {"action_id": "takopi-slack:action:preview", "command": "preview"},
            {"action_id": "takopi-slack:action:preview", "command": "status"},
        ],
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))


def test_from_config_action_blocks_file(tmp_path: Path) -> None:
    blocks_path = tmp_path / "blocks.json"
    blocks_path.write_text(
        '[{"type":"section","text":{"type":"plain_text","text":"ok"}}]',
        encoding="utf-8",
    )
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "action_blocks": f"@{blocks_path}",
    }
    settings = SlackTransportSettings.from_config(cfg, config_path=blocks_path)
    assert settings.action_blocks == [
        {"type": "section", "text": {"type": "plain_text", "text": "ok"}}
    ]


def test_from_config_invalid_action_blocks() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "action_blocks": '{"blocks": "nope"}',
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))
