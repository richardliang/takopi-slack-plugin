from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from takopi_slack_plugin import bridge
from takopi_slack_plugin.bridge import SlackBridgeConfig, build_startup_message
from takopi_slack_plugin.client import SlackAuth, SlackMessage
from takopi_slack_plugin.config import SlackTransportSettings
from takopi_slack_plugin.thread_sessions import SlackThreadSessionStore


class _FakeSlackClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.post_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.response_calls: list[dict[str, Any]] = []

    async def auth_test(self) -> SlackAuth:
        return SlackAuth(user_id="UBOT", user_name="takopi")

    async def post_message(
        self,
        *,
        channel_id: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        reply_broadcast: bool | None = None,
    ) -> SlackMessage:
        self.post_calls.append(
            {
                "channel_id": channel_id,
                "text": text,
                "blocks": blocks,
                "thread_ts": thread_ts,
                "reply_broadcast": reply_broadcast,
            }
        )
        return SlackMessage(
            ts=str(len(self.post_calls)),
            text=text,
            user=None,
            bot_id=None,
            subtype=None,
            thread_ts=thread_ts,
            channel_id=channel_id,
        )

    async def update_message(
        self,
        *,
        channel_id: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackMessage:
        self.update_calls.append(
            {"channel_id": channel_id, "ts": ts, "text": text, "blocks": blocks}
        )
        return SlackMessage(
            ts=ts,
            text=text,
            user=None,
            bot_id=None,
            subtype=None,
            thread_ts=None,
            channel_id=channel_id,
        )

    async def post_response(
        self,
        *,
        response_url: str,
        text: str,
        response_type: str = "ephemeral",
        replace_original: bool | None = None,
        delete_original: bool | None = None,
    ) -> None:
        self.response_calls.append(
            {
                "response_url": response_url,
                "text": text,
                "response_type": response_type,
                "replace_original": replace_original,
                "delete_original": delete_original,
            }
        )

    async def close(self) -> None:
        return None


class _FakeRuntime:
    def __init__(self, *, config_path: Path) -> None:
        self.config_path = config_path
        self.watch_config = True
        self.default_engine = "codex"

    @property
    def allowlist(self) -> set[str] | None:
        return None

    @property
    def engine_ids(self) -> tuple[str, ...]:
        return ("codex",)

    def available_engine_ids(self) -> tuple[str, ...]:
        return ("codex",)

    def missing_engine_ids(self) -> tuple[str, ...]:
        return ()

    def engine_ids_with_status(self, status: str) -> tuple[str, ...]:
        _ = status
        return ()

    def project_aliases(self) -> tuple[str, ...]:
        return ("demo",)


def _transport_cfg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bot_token": "xoxb-1",
        "channel_id": "C1",
        "app_token": "xapp-1",
        "allowed_user_ids": ["U1"],
        "reply_mode": "thread",
    }
    base.update(overrides)
    return base


def _build_cfg(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    transport_cfg: dict[str, Any] | None = None,
) -> tuple[SlackBridgeConfig, _FakeSlackClient, SlackThreadSessionStore]:
    monkeypatch.setattr(bridge, "SlackClient", _FakeSlackClient)
    config_path = tmp_path / "takopi.toml"
    config_path.write_text("watch_config = true\n", encoding="utf-8")
    settings = SlackTransportSettings.from_config(
        _transport_cfg(**(transport_cfg or {})),
        config_path=config_path,
    )
    runtime = _FakeRuntime(config_path=config_path)
    startup_msg = build_startup_message(runtime, startup_pwd=str(tmp_path))
    state, exec_cfg = bridge.create_reloadable_slack_state(
        settings,
        startup_pwd=str(tmp_path),
        startup_msg=startup_msg,
        final_notify=False,
    )
    thread_store = SlackThreadSessionStore(tmp_path / "slack_thread_sessions_state.json")
    cfg = SlackBridgeConfig(
        runtime=runtime,
        channel_id=settings.channel_id,
        exec_cfg=exec_cfg,
        state=state,
        thread_store=thread_store,
    )
    return cfg, cfg.client, thread_store


def _approval_payload(*, user_id: str, thread_id: str, action_id: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "channel": {"id": "C1"},
        "user": {"id": user_id},
        "response_url": "https://example.com/response",
        "message": {"ts": "1", "thread_ts": thread_id, "text": "approval"},
        "actions": [{"action_id": action_id, "value": thread_id}],
    }


@pytest.mark.anyio
async def test_request_approval_posts_button_message_and_persists_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, client, store = _build_cfg(tmp_path, monkeypatch=monkeypatch)

    created = await bridge._request_approval_for_message(
        cfg,
        message=SlackMessage(
            ts="111.222",
            text="<@UBOT> hello",
            user="U2",
            bot_id=None,
            subtype=None,
            thread_ts=None,
            channel_id="C1",
        ),
        text="hello",
    )

    assert created is True
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["thread_ts"] == "111.222"
    action_ids = [
        element["action_id"]
        for element in client.post_calls[0]["blocks"][-1]["elements"]
    ]
    assert action_ids == [
        bridge.APPROVE_REQUEST_ACTION_ID,
        bridge.DENY_REQUEST_ACTION_ID,
    ]

    approval = await store.get_pending_approval(channel_id="C1", thread_id="111.222")
    assert approval is not None
    assert approval.requester_user_id == "U2"
    assert approval.status == "pending"
    assert approval.approval_message_ts == "1"


@pytest.mark.anyio
async def test_request_approval_dedupes_existing_pending_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, client, _store = _build_cfg(tmp_path, monkeypatch=monkeypatch)
    message = SlackMessage(
        ts="111.222",
        text="<@UBOT> hello",
        user="U2",
        bot_id=None,
        subtype=None,
        thread_ts=None,
        channel_id="C1",
    )

    await bridge._request_approval_for_message(cfg, message=message, text="hello")
    await bridge._request_approval_for_message(cfg, message=message, text="hello")

    assert len(client.post_calls) == 1


@pytest.mark.anyio
async def test_approval_action_runs_saved_request_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, client, store = _build_cfg(tmp_path, monkeypatch=monkeypatch)
    executed: list[tuple[str | None, str]] = []

    async def fake_safe_handle(_cfg, message, text: str, _running_tasks) -> None:
        executed.append((message.user, text))

    monkeypatch.setattr(bridge, "_safe_handle_slack_message", fake_safe_handle)
    await bridge._request_approval_for_message(
        cfg,
        message=SlackMessage(
            ts="111.222",
            text="<@UBOT> hello",
            user="U2",
            bot_id=None,
            subtype=None,
            thread_ts=None,
            channel_id="C1",
        ),
        text="hello",
    )

    handled = await bridge._handle_approval_approve_action(
        cfg,
        _approval_payload(
            user_id="U1",
            thread_id="111.222",
            action_id=bridge.APPROVE_REQUEST_ACTION_ID,
        ),
        object(),
    )

    assert handled is True
    assert executed == [("U2", "hello")]
    assert client.update_calls[-1]["text"].startswith("approval granted")
    approval = await store.get_pending_approval(channel_id="C1", thread_id="111.222")
    assert approval is not None
    assert approval.status == "approved"
    assert approval.decided_by_user_id == "U1"


@pytest.mark.anyio
async def test_deny_action_marks_request_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, client, store = _build_cfg(tmp_path, monkeypatch=monkeypatch)
    await bridge._request_approval_for_message(
        cfg,
        message=SlackMessage(
            ts="111.222",
            text="<@UBOT> hello",
            user="U2",
            bot_id=None,
            subtype=None,
            thread_ts=None,
            channel_id="C1",
        ),
        text="hello",
    )

    handled = await bridge._handle_approval_deny_action(
        cfg,
        _approval_payload(
            user_id="U1",
            thread_id="111.222",
            action_id=bridge.DENY_REQUEST_ACTION_ID,
        ),
    )

    assert handled is True
    assert client.update_calls[-1]["text"].startswith("approval denied")
    approval = await store.get_pending_approval(channel_id="C1", thread_id="111.222")
    assert approval is not None
    assert approval.status == "denied"


@pytest.mark.anyio
async def test_unauthorized_user_cannot_click_approval_buttons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, client, store = _build_cfg(tmp_path, monkeypatch=monkeypatch)
    await bridge._request_approval_for_message(
        cfg,
        message=SlackMessage(
            ts="111.222",
            text="<@UBOT> hello",
            user="U2",
            bot_id=None,
            subtype=None,
            thread_ts=None,
            channel_id="C1",
        ),
        text="hello",
    )

    await bridge._handle_interactive(
        cfg,
        _approval_payload(
            user_id="U2",
            thread_id="111.222",
            action_id=bridge.APPROVE_REQUEST_ACTION_ID,
        ),
        object(),
    )

    assert client.response_calls[-1]["text"] == "this Slack user is not allowed to use Takopi."
    approval = await store.get_pending_approval(channel_id="C1", thread_id="111.222")
    assert approval is not None
    assert approval.status == "pending"
