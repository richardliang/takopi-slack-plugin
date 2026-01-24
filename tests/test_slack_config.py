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
        "files": {
            "enabled": True,
            "auto_put": False,
            "auto_put_mode": "prompt",
            "uploads_dir": "incoming",
            "allowed_user_ids": ["U123"],
            "max_upload_bytes": 1024,
            "max_download_bytes": 2048,
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


def test_from_config_invalid_uploads_dir() -> None:
    cfg = {
        "bot_token": "xoxb-1",
        "channel_id": "C123",
        "app_token": "xapp-1",
        "files": {"uploads_dir": "/abs/path"},
    }
    with pytest.raises(ConfigError):
        SlackTransportSettings.from_config(cfg, config_path=Path("/tmp/x"))
