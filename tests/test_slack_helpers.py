from __future__ import annotations

import json

from takopi.api import RunContext
from takopi_slack_plugin.bridge import (
    _coerce_socket_payload,
    _extract_command_text,
    _extract_inline_command,
    _extract_payload_user_id,
    _extract_slash_payload_command,
    _format_context_directive,
    _is_allowed_user,
    _parse_thread_ts,
    _should_skip_message,
    _strip_bot_mention,
    split_command_args,
)
from takopi_slack_plugin.client import SlackMessage


def test_split_command_args_quoted() -> None:
    assert split_command_args('/run "hello world"') == ("/run", "hello world")


def test_split_command_args_fallback() -> None:
    text = '/run "unterminated'
    assert split_command_args(text) == ("/run", '"unterminated')


def test_extract_command_text() -> None:
    tokens = ("/preview", "start")
    command_id, args_text = _extract_command_text(tokens, "/preview start --port 3000")
    assert command_id == "preview"
    assert args_text == "start --port 3000"


def test_extract_inline_command() -> None:
    prompt = "please /preview start --port 3000"
    result = _extract_inline_command(prompt, allowed_commands={"preview"})
    assert result == ("preview", "start --port 3000", "/preview start --port 3000")


def test_extract_inline_command_ignores_unknown() -> None:
    prompt = "run /unknown now"
    assert _extract_inline_command(prompt, allowed_commands={"preview"}) is None


def test_extract_slash_payload_command() -> None:
    assert _extract_slash_payload_command("/takopi-status") == "status"
    assert _extract_slash_payload_command("/takopi_status") == "status"
    assert _extract_slash_payload_command("/status") is None


def test_extract_payload_user_id() -> None:
    assert _extract_payload_user_id({"user_id": "U123"}) == "U123"
    assert _extract_payload_user_id({"user": {"id": "U456"}}) == "U456"
    assert _extract_payload_user_id({"user": {"id": " "}}) is None
    assert _extract_payload_user_id({}) is None


def test_is_allowed_user() -> None:
    assert _is_allowed_user([], "U123")
    assert _is_allowed_user(["U123"], "U123")
    assert not _is_allowed_user(["U123"], "U999")
    assert not _is_allowed_user(["U123"], None)


def test_format_context_directive() -> None:
    assert _format_context_directive(None) is None
    context = RunContext(project="proj", branch="feat")
    assert _format_context_directive(context) == "/proj @feat"


def test_parse_thread_ts() -> None:
    assert _parse_thread_ts("123.45") == "123.45"
    assert _parse_thread_ts(123) is None


def test_strip_bot_mention() -> None:
    assert (
        _strip_bot_mention("<@U123> hello", bot_user_id="U123", bot_name=None)
        == "hello"
    )
    assert (
        _strip_bot_mention("@takopi please", bot_user_id=None, bot_name="takopi")
        == "please"
    )


def test_coerce_socket_payload() -> None:
    payload = {"type": "event"}
    assert _coerce_socket_payload(payload) == payload

    raw_json = json.dumps({"type": "event", "value": 1})
    assert _coerce_socket_payload(raw_json) == {"type": "event", "value": 1}

    form_payload = "payload=" + json.dumps({"type": "interactive"})
    assert _coerce_socket_payload(form_payload) == {"type": "interactive"}

    raw_form = "token=abc&text=hello"
    assert _coerce_socket_payload(raw_form) == {"token": "abc", "text": "hello"}


def test_should_skip_message() -> None:
    assert _should_skip_message(
        SlackMessage(ts="", text="hi", user="U1", bot_id=None, subtype=None, thread_ts=None),
        bot_user_id=None,
    )
    assert _should_skip_message(
        SlackMessage(
            ts="1",
            text="hi",
            user="U1",
            bot_id=None,
            subtype="bot_message",
            thread_ts=None,
        ),
        bot_user_id=None,
    )
    assert not _should_skip_message(
        SlackMessage(
            ts="1",
            text="",
            user="U2",
            bot_id=None,
            subtype="file_share",
            thread_ts=None,
            files=[{"id": "F1", "url_private": "https://example.com"}],
        ),
        bot_user_id=None,
    )
    assert _should_skip_message(
        SlackMessage(
            ts="1",
            text="hi",
            user="U1",
            bot_id=None,
            subtype=None,
            thread_ts=None,
        ),
        bot_user_id="U1",
    )
    assert not _should_skip_message(
        SlackMessage(
            ts="1",
            text="hi",
            user="U2",
            bot_id=None,
            subtype=None,
            thread_ts=None,
        ),
        bot_user_id="U1",
    )
