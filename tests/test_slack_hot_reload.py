from __future__ import annotations

import json
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from takopi_slack_plugin import bridge
from takopi_slack_plugin.bridge import (
    CommandContext,
    SlackBridgeConfig,
    build_startup_message,
)
from takopi_slack_plugin.client import SlackAuth, SlackMessage
from takopi_slack_plugin.config import SlackTransportSettings


class _ImmediateOutbox:
    async def enqueue(self, *, key, op, wait: bool = True):
        result = await op.execute()
        op.set_result(result)
        return result

    async def drop_pending(self, *, key) -> None:
        _ = key
        return None

    async def close(self, *, drain: bool = False) -> None:
        _ = drain
        return None


class _FakeSlackClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.auth_calls: list[str] = []
        self.post_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.response_calls: list[dict[str, Any]] = []
        self.close_calls = 0

    async def auth_test(self) -> SlackAuth:
        self.auth_calls.append(self.token)
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

    async def delete_message(self, *, channel_id: str, ts: str) -> bool:
        self.delete_calls.append({"channel_id": channel_id, "ts": ts})
        return True

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
        self.close_calls += 1


@dataclass(slots=True)
class _FakeRuntime:
    config_path: Path | None
    watch_config: bool = True
    default_engine: str = "codex"

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


class _ReloadSettings:
    def __init__(self, transport_cfg: dict[str, Any]) -> None:
        self._transport_cfg = transport_cfg

    def transport_config(
        self,
        transport_id: str,
        *,
        config_path: Path,
    ) -> dict[str, Any]:
        _ = config_path
        assert transport_id == "slack"
        return self._transport_cfg


class _WatchController:
    def __init__(self) -> None:
        self.started = anyio.Event()
        self._done = anyio.Event()
        self.on_reload = None
        self.invalid_attempts = 0

    async def __call__(
        self,
        *,
        config_path: Path,
        runtime,
        default_engine_override: str | None = None,
        on_reload=None,
    ) -> None:
        _ = config_path, runtime, default_engine_override
        self.on_reload = on_reload
        self.started.set()
        await self._done.wait()

    async def reload(
        self,
        transport_cfg: dict[str, Any],
        *,
        config_path: Path,
    ) -> None:
        assert self.on_reload is not None
        await self.on_reload(
            types.SimpleNamespace(
                settings=_ReloadSettings(transport_cfg),
                config_path=config_path,
            )
        )

    async def invalid_reload(self) -> None:
        self.invalid_attempts += 1

    def stop(self) -> None:
        self._done.set()


class _FakeWebSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.sent_payloads: list[dict[str, Any]] = []
        self.close_calls = 0
        self._send_stream, self._recv_stream = anyio.create_memory_object_stream(100)

    async def __aenter__(self) -> "_FakeWebSocket":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = exc_type, exc, tb
        await self._send_stream.aclose()
        await self._recv_stream.aclose()

    async def recv(self) -> str:
        payload = await self._recv_stream.receive()
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, (dict, list)):
            return json.dumps(payload)
        return str(payload)

    async def send(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    async def close(self) -> None:
        self.close_calls += 1
        await self._send_stream.send(OSError("socket closed"))

    async def push(self, payload: dict[str, Any]) -> None:
        await self._send_stream.send(payload)


class _WebSocketFactory:
    def __init__(self) -> None:
        self.sessions: list[_FakeWebSocket] = []

    def connect(self, url: str, **kwargs) -> _FakeWebSocket:
        _ = kwargs
        session = _FakeWebSocket(url)
        self.sessions.append(session)
        return session


def _transport_cfg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bot_token": "xoxb-1",
        "channel_id": "C1",
        "app_token": "xapp-1",
        "allowed_user_ids": ["U1"],
        "plugin_channels": {},
        "reply_mode": "thread",
    }
    base.update(overrides)
    return base


def _build_cfg(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    transport_cfg: dict[str, Any],
) -> tuple[SlackBridgeConfig, _FakeRuntime, Path]:
    monkeypatch.setattr(bridge, "SlackClient", _FakeSlackClient)
    config_path = tmp_path / "takopi.toml"
    config_path.write_text("watch_config = true\n", encoding="utf-8")
    settings = SlackTransportSettings.from_config(
        transport_cfg,
        config_path=config_path,
    )
    runtime = _FakeRuntime(config_path=config_path, watch_config=True)
    startup_msg = build_startup_message(runtime, startup_pwd=str(tmp_path))
    state, exec_cfg = bridge.create_reloadable_slack_state(
        settings,
        startup_pwd=str(tmp_path),
        startup_msg=startup_msg,
        final_notify=False,
    )
    state.transport._outbox = _ImmediateOutbox()
    cfg = SlackBridgeConfig(
        runtime=runtime,
        channel_id=settings.channel_id,
        exec_cfg=exec_cfg,
        state=state,
        thread_store=None,
    )
    return cfg, runtime, config_path


def _event_envelope(*, ts: str, user_id: str) -> dict[str, Any]:
    return {
        "type": "events_api",
        "payload": {
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "ts": ts,
                "text": "<@UBOT> hello",
                "user": user_id,
            }
        },
    }


def _slash_envelope() -> dict[str, Any]:
    return {
        "type": "slash_commands",
        "payload": {
            "channel_id": "C1",
            "user_id": "U1",
            "command": "/takopi-cron",
            "text": "summary",
            "response_url": "https://example.com/response",
        },
    }


async def _wait_until(
    predicate,
    *,
    timeout: float = 2.0,
) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_allowed_user_ids_reload_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _runtime, config_path = _build_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        transport_cfg=_transport_cfg(allowed_user_ids=["U1"]),
    )
    watch = _WatchController()
    sockets = _WebSocketFactory()
    open_calls: list[str] = []
    handled: list[tuple[str | None, str]] = []

    async def fake_open_socket_url(token: str) -> str:
        open_calls.append(token)
        return f"wss://{token}"

    async def fake_safe_handle(_cfg, msg, text: str, _running_tasks) -> None:
        handled.append((msg.user, text))

    monkeypatch.setattr(bridge, "core_watch_config", watch)
    monkeypatch.setattr(bridge, "open_socket_url", fake_open_socket_url)
    monkeypatch.setattr(bridge.websockets, "connect", sockets.connect)
    monkeypatch.setattr(bridge, "_safe_handle_slack_message", fake_safe_handle)

    async def _run() -> None:
        await bridge.run_main_loop(
            cfg,
            watch_config=True,
            transport_id="slack",
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        await _wait_until(lambda: watch.started.is_set() and len(sockets.sessions) == 1)

        await sockets.sessions[0].push(_event_envelope(ts="1", user_id="U1"))
        await _wait_until(lambda: len(handled) == 1)

        await watch.reload(
            _transport_cfg(allowed_user_ids=["U2"]),
            config_path=config_path,
        )
        await sockets.sessions[0].push(_event_envelope(ts="2", user_id="U1"))
        await anyio.sleep(0.1)

        assert handled == [("U1", "hello")]
        assert open_calls == ["xapp-1"]

        watch.stop()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_plugin_channels_reroute_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _runtime, config_path = _build_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        transport_cfg=_transport_cfg(plugin_channels={"cron": "C100"}),
    )
    watch = _WatchController()
    sockets = _WebSocketFactory()
    routed_channels: list[str | None] = []

    async def fake_open_socket_url(token: str) -> str:
        return f"wss://{token}"

    async def fake_dispatch_command(_cfg, **kwargs) -> bool:
        routed_channels.append(kwargs.get("output_channel_id"))
        return True

    async def fake_resolve_command_context(
        _cfg,
        *,
        channel_id: str,
        thread_id: str,
    ) -> CommandContext:
        _ = channel_id, thread_id

        async def _noop_resolver(_engine_id: str):
            return None

        return CommandContext(
            default_context=None,
            default_engine_override=None,
            engine_overrides_resolver=_noop_resolver,
            on_thread_known=None,
        )

    monkeypatch.setattr(bridge, "core_watch_config", watch)
    monkeypatch.setattr(bridge, "open_socket_url", fake_open_socket_url)
    monkeypatch.setattr(bridge.websockets, "connect", sockets.connect)
    monkeypatch.setattr(bridge, "dispatch_command", fake_dispatch_command)
    monkeypatch.setattr(
        bridge,
        "_resolve_command_context",
        fake_resolve_command_context,
    )

    async def _run() -> None:
        await bridge.run_main_loop(
            cfg,
            watch_config=True,
            transport_id="slack",
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        await _wait_until(lambda: watch.started.is_set() and len(sockets.sessions) == 1)

        await sockets.sessions[0].push(_slash_envelope())
        await _wait_until(lambda: len(routed_channels) == 1)
        assert routed_channels == ["C100"]

        await watch.reload(
            _transport_cfg(plugin_channels={"cron": "C200"}),
            config_path=config_path,
        )
        await sockets.sessions[0].push(_slash_envelope())
        await _wait_until(lambda: len(routed_channels) == 2)
        assert routed_channels == ["C100", "C200"]

        watch.stop()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_invalid_toml_keeps_bot_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _runtime, _config_path = _build_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        transport_cfg=_transport_cfg(allowed_user_ids=["U1"]),
    )
    watch = _WatchController()
    sockets = _WebSocketFactory()
    open_calls: list[str] = []
    handled: list[str] = []

    async def fake_open_socket_url(token: str) -> str:
        open_calls.append(token)
        return f"wss://{token}"

    async def fake_safe_handle(_cfg, _msg, text: str, _running_tasks) -> None:
        handled.append(text)

    monkeypatch.setattr(bridge, "core_watch_config", watch)
    monkeypatch.setattr(bridge, "open_socket_url", fake_open_socket_url)
    monkeypatch.setattr(bridge.websockets, "connect", sockets.connect)
    monkeypatch.setattr(bridge, "_safe_handle_slack_message", fake_safe_handle)

    async def _run() -> None:
        await bridge.run_main_loop(
            cfg,
            watch_config=True,
            transport_id="slack",
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        await _wait_until(lambda: watch.started.is_set() and len(sockets.sessions) == 1)

        await sockets.sessions[0].push(_event_envelope(ts="1", user_id="U1"))
        await _wait_until(lambda: handled == ["hello"])

        await watch.invalid_reload()
        await sockets.sessions[0].push(_event_envelope(ts="2", user_id="U1"))
        await _wait_until(lambda: handled == ["hello", "hello"])

        assert watch.invalid_attempts == 1
        assert open_calls == ["xapp-1"]

        watch.stop()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_app_token_reload_reconnects_socket_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _runtime, config_path = _build_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        transport_cfg=_transport_cfg(app_token="xapp-1"),
    )
    watch = _WatchController()
    sockets = _WebSocketFactory()
    open_calls: list[str] = []

    async def fake_open_socket_url(token: str) -> str:
        open_calls.append(token)
        return f"wss://{token}"

    monkeypatch.setattr(bridge, "core_watch_config", watch)
    monkeypatch.setattr(bridge, "open_socket_url", fake_open_socket_url)
    monkeypatch.setattr(bridge.websockets, "connect", sockets.connect)

    async def _run() -> None:
        await bridge.run_main_loop(
            cfg,
            watch_config=True,
            transport_id="slack",
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        await _wait_until(lambda: watch.started.is_set() and len(sockets.sessions) == 1)

        await watch.reload(
            _transport_cfg(app_token="xapp-2"),
            config_path=config_path,
        )
        await _wait_until(lambda: len(sockets.sessions) == 2)

        assert open_calls[:2] == ["xapp-1", "xapp-2"]
        assert sockets.sessions[0].close_calls == 1
        assert cfg.state.app_token == "xapp-2"

        watch.stop()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_bot_token_reload_rebuilds_transport_and_closes_old_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, runtime, config_path = _build_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        transport_cfg=_transport_cfg(bot_token="xoxb-1"),
    )
    initial_client = cfg.state.client
    initial_outbox = cfg.state.transport._outbox
    settings = SlackTransportSettings.from_config(
        _transport_cfg(bot_token="xoxb-2"),
        config_path=config_path,
    )

    await bridge.reload_slack_settings(cfg.state, settings, runtime)

    assert cfg.state.client is not initial_client
    assert cfg.state.client.token == "xoxb-2"
    assert cfg.state.transport._outbox is not initial_outbox
    assert initial_client.close_calls == 1


@pytest.mark.anyio
async def test_action_block_reload_rebuilds_transport_without_closing_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, runtime, config_path = _build_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        transport_cfg=_transport_cfg(),
    )
    initial_client = cfg.state.client
    initial_outbox = cfg.state.transport._outbox
    settings = SlackTransportSettings.from_config(
        _transport_cfg(
            action_blocks="""
[
  {
    "type": "actions",
    "elements": [
      {
        "type": "button",
        "text": { "type": "plain_text", "text": "Archive" },
        "action_id": "takopi-slack:archive"
      }
    ]
  }
]
"""
        ),
        config_path=config_path,
    )

    await bridge.reload_slack_settings(cfg.state, settings, runtime)

    assert cfg.state.client is initial_client
    assert cfg.state.transport._outbox is not initial_outbox
    assert initial_client.close_calls == 0
