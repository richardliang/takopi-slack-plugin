from __future__ import annotations

import copy
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import parse_qs

import anyio
import websockets
from websockets.exceptions import WebSocketException

from takopi.api import (
    ConfigError,
    DirectiveError,
    ExecBridgeConfig,
    MessageRef,
    RenderedMessage,
    RunContext,
    RunningTasks,
    SendOptions,
    TransportRuntime,
    get_logger,
)
from takopi.config_watch import ConfigReload, watch_config as core_watch_config
from takopi.directives import parse_directives
from takopi.ids import RESERVED_COMMAND_IDS
from takopi.plugins import COMMAND_GROUP, list_ids
from takopi.runners.run_options import EngineRunOptions

from .client import SlackApiError, SlackClient, SlackMessage, open_socket_url
from .commands import dispatch_command, split_command_args
from .config import SlackActionHandler, SlackFilesSettings, SlackTransportSettings
from .engine import run_engine, send_plain
from .commands.file_transfer import (
    extract_files,
    handle_file_command,
    handle_file_uploads,
)
from .outbox import DELETE_PRIORITY, EDIT_PRIORITY, SEND_PRIORITY, OutboxOp, SlackOutbox
from .overrides import REASONING_LEVELS, is_valid_reasoning_level, supports_reasoning
from .thread_sessions import (
    PendingApprovalSnapshot,
    SlackThreadSessionStore,
    ThreadSnapshot,
    WorktreeSnapshot,
)

logger = get_logger(__name__)

MAX_SLACK_TEXT = 3900
MAX_BLOCK_TEXT = 2800
CANCEL_ARCHIVE_ACTION_ID = "takopi-slack:archive-cancel"
ARCHIVE_ACTION_ID = "takopi-slack:archive"
CONFIRM_ARCHIVE_ACTION_ID = "takopi-slack:archive-confirm"
APPROVE_REQUEST_ACTION_ID = "takopi-slack:approval-approve"
DENY_REQUEST_ACTION_ID = "takopi-slack:approval-deny"
CANCEL_ACTION_ID = "takopi-slack:cancel"
APPROVAL_TTL_S = 1800.0
INLINE_COMMAND_RE = re.compile(
    r"(^|\s)(?P<token>/(?P<cmd>[a-z0-9_]{1,32}))",
    re.IGNORECASE,
)
THREAD_SEND_ERRORS = {
    "invalid_thread_ts",
    "invalid_timestamp",
    "message_not_found",
    "thread_ts_not_found",
}


def _slack_state(cfg: SlackBridgeConfig | Any) -> ReloadableSlackState | Any:
    return getattr(cfg, "state", cfg)


class SlackPresenter:
    def __init__(
        self,
        *,
        message_overflow: str = "trim",
        max_chars: int = MAX_SLACK_TEXT,
        max_actions: int = 5,
    ) -> None:
        self._message_overflow = message_overflow
        self._max_chars = max(1, int(max_chars))
        self._max_actions = max(0, int(max_actions))

    def render_progress(
        self,
        state,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        text = _render_progress_text(
            state,
            elapsed_s=elapsed_s,
            label=label,
            max_actions=self._max_actions,
        )
        rendered = RenderedMessage(text=_trim_text(text, self._max_chars))
        show_cancel = not _is_cancelled_label(label)
        rendered.extra["show_cancel"] = show_cancel
        if not show_cancel:
            rendered.extra["clear_blocks"] = True
        return rendered

    def render_final(
        self,
        state,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        text = _render_final_text(
            state,
            elapsed_s=elapsed_s,
            status=status,
            answer=answer,
        )
        if self._message_overflow == "split":
            chunks = _split_text(text, self._max_chars)
            message = RenderedMessage(text=chunks[0])
            message.extra["clear_blocks"] = True
            message.extra["show_archive"] = True
            if len(chunks) > 1:
                message.extra["followups"] = [
                    RenderedMessage(text=chunk) for chunk in chunks[1:]
                ]
            return message
        rendered = RenderedMessage(text=_trim_text(text, self._max_chars))
        rendered.extra["clear_blocks"] = True
        rendered.extra["show_archive"] = True
        return rendered


def build_startup_message(
    runtime: TransportRuntime,
    *,
    startup_pwd: str,
) -> str:
    available_engines = list(runtime.available_engine_ids())
    missing_engines = list(runtime.missing_engine_ids())
    misconfigured_engines = list(runtime.engine_ids_with_status("bad_config"))
    failed_engines = list(runtime.engine_ids_with_status("load_error"))

    engine_list = ", ".join(available_engines) if available_engines else "none"

    notes: list[str] = []
    if missing_engines:
        notes.append(f"not installed: {', '.join(missing_engines)}")
    if misconfigured_engines:
        notes.append(f"misconfigured: {', '.join(misconfigured_engines)}")
    if failed_engines:
        notes.append(f"failed to load: {', '.join(failed_engines)}")
    if notes:
        engine_list = f"{engine_list} ({'; '.join(notes)})"

    project_aliases = sorted(set(runtime.project_aliases()), key=str.lower)
    project_list = ", ".join(project_aliases) if project_aliases else "none"

    return (
        "takopi is ready\n\n"
        f"default: `{runtime.default_engine}`\n"
        f"agents: `{engine_list}`\n"
        f"projects: `{project_list}`\n"
        f"working in: `{startup_pwd}`"
    )


class ReloadableSlackPresenter:
    def __init__(self, state: "ReloadableSlackState") -> None:
        self._state = state

    def render_progress(
        self,
        state,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        return self._state.presenter.render_progress(
            state,
            elapsed_s=elapsed_s,
            label=label,
        )

    def render_final(
        self,
        state,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        return self._state.presenter.render_final(
            state,
            elapsed_s=elapsed_s,
            status=status,
            answer=answer,
        )


class ReloadableSlackTransport:
    def __init__(self, state: "ReloadableSlackState") -> None:
        self._state = state

    async def close(self) -> None:
        await self._state.transport.close()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        return await self._state.transport.send(
            channel_id=channel_id,
            message=message,
            options=options,
        )

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        return await self._state.transport.edit(
            ref=ref,
            message=message,
            wait=wait,
        )

    async def delete(self, *, ref: MessageRef) -> bool:
        return await self._state.transport.delete(ref=ref)


@dataclass(slots=True)
class ReloadableSlackState:
    startup_pwd: str
    client: SlackClient
    transport: "SlackTransport"
    presenter: SlackPresenter
    bot_token: str
    app_token: str
    allowed_user_ids: list[str]
    allowed_channel_ids: list[str]
    plugin_channels: dict[str, str]
    reply_mode: Literal["thread", "channel"]
    message_overflow: Literal["trim", "split"]
    startup_msg: str
    files: SlackFilesSettings
    action_handlers: list[SlackActionHandler] = field(default_factory=list)
    action_blocks: list[dict[str, Any]] | None = None
    stale_worktree_reminder: bool = False
    stale_worktree_hours: float = 24.0
    stale_worktree_check_interval_s: float = 600.0
    needs_reconnect: bool = False
    reconnect_socket: Callable[[], Awaitable[None]] | None = None

    async def request_reconnect(self) -> None:
        self.needs_reconnect = True
        reconnect = self.reconnect_socket
        if reconnect is None:
            return
        try:
            await reconnect()
        except Exception as exc:  # pragma: no cover - safety net
            logger.warning(
                "slack.socket.reconnect_failed",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )


def create_reloadable_slack_state(
    settings: SlackTransportSettings,
    *,
    final_notify: bool,
    startup_msg: str,
    startup_pwd: str,
) -> tuple[ReloadableSlackState, ExecBridgeConfig]:
    client = SlackClient(settings.bot_token)
    state = ReloadableSlackState(
        startup_pwd=startup_pwd,
        client=client,
        transport=SlackTransport(
            client,
            action_blocks=copy.deepcopy(settings.action_blocks),
        ),
        presenter=SlackPresenter(message_overflow=settings.message_overflow),
        bot_token=settings.bot_token,
        app_token=settings.app_token,
        allowed_user_ids=list(settings.allowed_user_ids),
        allowed_channel_ids=list(settings.allowed_channel_ids),
        plugin_channels=dict(settings.plugin_channels),
        reply_mode=settings.reply_mode,
        message_overflow=settings.message_overflow,
        startup_msg=startup_msg,
        files=settings.files,
        action_handlers=list(settings.action_handlers),
        action_blocks=copy.deepcopy(settings.action_blocks),
        stale_worktree_reminder=settings.stale_worktree_reminder,
        stale_worktree_hours=settings.stale_worktree_hours,
        stale_worktree_check_interval_s=settings.stale_worktree_check_interval_s,
    )
    exec_cfg = ExecBridgeConfig(
        transport=ReloadableSlackTransport(state),
        presenter=ReloadableSlackPresenter(state),
        final_notify=final_notify,
    )
    return state, exec_cfg


async def reload_slack_settings(
    state: ReloadableSlackState,
    settings: SlackTransportSettings,
    runtime: TransportRuntime,
) -> None:
    bot_token_changed = settings.bot_token != state.bot_token
    app_token_changed = settings.app_token != state.app_token
    action_blocks = copy.deepcopy(settings.action_blocks)
    action_blocks_changed = action_blocks != state.action_blocks
    message_overflow_changed = settings.message_overflow != state.message_overflow

    client = state.client
    retired_transport: SlackTransport | None = None
    close_retired_client = False
    if bot_token_changed:
        client = SlackClient(settings.bot_token)
        state.client = client

    if bot_token_changed or action_blocks_changed:
        retired_transport = state.transport
        close_retired_client = bot_token_changed
        state.transport = SlackTransport(
            client,
            action_blocks=copy.deepcopy(action_blocks),
        )
    if message_overflow_changed:
        state.presenter = SlackPresenter(
            message_overflow=settings.message_overflow
        )

    state.bot_token = settings.bot_token
    state.app_token = settings.app_token
    state.allowed_user_ids = list(settings.allowed_user_ids)
    state.allowed_channel_ids = list(settings.allowed_channel_ids)
    state.plugin_channels = dict(settings.plugin_channels)
    state.reply_mode = settings.reply_mode
    state.message_overflow = settings.message_overflow
    state.startup_msg = build_startup_message(
        runtime,
        startup_pwd=state.startup_pwd,
    )
    state.files = settings.files
    state.action_handlers = list(settings.action_handlers)
    state.action_blocks = action_blocks
    state.stale_worktree_reminder = settings.stale_worktree_reminder
    state.stale_worktree_hours = settings.stale_worktree_hours
    state.stale_worktree_check_interval_s = (
        settings.stale_worktree_check_interval_s
    )

    if bot_token_changed or app_token_changed:
        await state.request_reconnect()
    if retired_transport is not None:
        await retired_transport.close(
            drain=True,
            close_client=close_retired_client,
        )


@dataclass(frozen=True, slots=True)
class SlackBridgeConfig:
    runtime: TransportRuntime
    channel_id: str
    exec_cfg: ExecBridgeConfig
    state: ReloadableSlackState
    thread_store: SlackThreadSessionStore | None = None

    @property
    def client(self) -> SlackClient:
        return self.state.client

    @property
    def files(self) -> SlackFilesSettings:
        return self.state.files

    @property
    def reply_mode(self) -> Literal["thread", "channel"]:
        return self.state.reply_mode

    @property
    def app_token(self) -> str:
        return self.state.app_token

    @property
    def startup_msg(self) -> str:
        return self.state.startup_msg

    @property
    def action_handlers(self) -> list[SlackActionHandler]:
        return self.state.action_handlers

    @property
    def action_blocks(self) -> list[dict[str, Any]] | None:
        return self.state.action_blocks

    @property
    def stale_worktree_reminder(self) -> bool:
        return self.state.stale_worktree_reminder

    @property
    def stale_worktree_hours(self) -> float:
        return self.state.stale_worktree_hours

    @property
    def stale_worktree_check_interval_s(self) -> float:
        return self.state.stale_worktree_check_interval_s


@dataclass(frozen=True, slots=True)
class CommandContext:
    default_context: RunContext | None
    default_engine_override: str | None
    engine_overrides_resolver: Callable[[str], Awaitable[EngineRunOptions | None]]
    on_thread_known: Callable[[Any, anyio.Event], Awaitable[None]] | None


@dataclass(frozen=True, slots=True)
class SlashCommandRequest:
    channel_id: str
    user_id: str | None
    response_url: str | None
    session_thread_id: str
    response_thread_id: str | None
    command_id: str
    args_text: str
    tokens: tuple[str, ...]


class SlackTransport:
    def __init__(
        self,
        client: SlackClient,
        *,
        action_blocks: list[dict[str, Any]] | None = None,
        outbox: SlackOutbox | None = None,
    ) -> None:
        self._client = client
        self._outbox = outbox or SlackOutbox()
        self._send_counter = 0
        self._action_blocks = action_blocks

    @staticmethod
    def _extract_followups(message: RenderedMessage) -> list[RenderedMessage]:
        followups = message.extra.get("followups")
        if not isinstance(followups, list):
            return []
        return [item for item in followups if isinstance(item, RenderedMessage)]

    def _next_send_key(self, channel_id: str) -> tuple[str, str, int]:
        self._send_counter += 1
        return ("send", channel_id, self._send_counter)

    @staticmethod
    def _edit_key(channel_id: str, ts: str) -> tuple[str, str, str]:
        return ("edit", channel_id, ts)

    @staticmethod
    def _delete_key(channel_id: str, ts: str) -> tuple[str, str, str]:
        return ("delete", channel_id, ts)

    def _prepare_blocks(
        self,
        message: RenderedMessage,
        *,
        allow_clear: bool,
        thread_id: str | None,
    ) -> list[dict[str, Any]] | None:
        extra = message.extra
        blocks = extra.get("blocks")
        if isinstance(blocks, list):
            return blocks
        if extra.get("show_cancel"):
            return _build_cancel_blocks(message.text)
        if extra.get("show_archive"):
            return _build_archive_blocks(
                message.text,
                thread_id=thread_id,
                action_blocks=self._action_blocks,
            )
        if allow_clear and extra.get("clear_blocks"):
            return []
        return None

    async def close(
        self,
        *,
        drain: bool = False,
        close_client: bool = True,
    ) -> None:
        await self._outbox.close(drain=drain)
        if close_client:
            await self._client.close()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        channel = str(channel_id)
        thread_ts = None
        if options is not None and options.thread_id is not None:
            thread_ts = str(options.thread_id)
        followups = self._extract_followups(message)
        blocks = self._prepare_blocks(
            message, allow_clear=False, thread_id=thread_ts
        )
        sent = await self._enqueue_send(
            channel_id=channel,
            text=message.text,
            blocks=blocks,
            thread_ts=thread_ts,
        )
        ref = MessageRef(
            channel_id=channel,
            message_id=sent.ts,
            raw=sent,
            thread_id=thread_ts,
        )
        if options is not None and options.replace is not None:
            await self.delete(
                ref=MessageRef(
                    channel_id=channel,
                    message_id=str(options.replace.message_id),
                    thread_id=thread_ts,
                )
            )
        followup_thread = None
        if message.extra.get("followup_thread_id") is not None:
            followup_thread = str(message.extra.get("followup_thread_id"))
        if followup_thread is None:
            followup_thread = thread_ts
        for followup in followups:
            await self._enqueue_send(
                channel_id=channel,
                text=followup.text,
                blocks=None,
                thread_ts=followup_thread,
            )
        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        blocks = self._prepare_blocks(
            message, allow_clear=True, thread_id=ref.thread_id
        )
        updated = await self._enqueue_edit(
            channel_id=str(ref.channel_id),
            ts=str(ref.message_id),
            text=message.text,
            blocks=blocks,
            wait=wait,
        )
        if updated is None:
            return ref if not wait else None
        return MessageRef(
            channel_id=ref.channel_id,
            message_id=updated.ts,
            raw=updated,
            thread_id=ref.thread_id,
        )

    async def delete(self, *, ref: MessageRef) -> bool:
        return await self._enqueue_delete(
            channel_id=str(ref.channel_id),
            ts=str(ref.message_id),
        )

    async def _enqueue_send(
        self,
        *,
        channel_id: str,
        text: str,
        blocks: list[dict[str, Any]] | None,
        thread_ts: str | None,
    ) -> SlackMessage:
        async def execute() -> SlackMessage:
            try:
                return await self._client.post_message(
                    channel_id=channel_id,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_ts,
                )
            except SlackApiError as exc:
                if thread_ts is None:
                    logger.warning(
                        "slack.send_failed",
                        channel_id=channel_id,
                        error=exc.error,
                        status_code=exc.status_code,
                    )
                    raise
                logger.warning(
                    "slack.thread_send_failed",
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    error=exc.error,
                    status_code=exc.status_code,
                )
                if exc.error not in THREAD_SEND_ERRORS:
                    raise
                return await self._client.post_message(
                    channel_id=channel_id,
                    text=text,
                    blocks=blocks,
                )

        key = self._next_send_key(channel_id)
        op = OutboxOp(
            execute=execute,
            priority=SEND_PRIORITY,
            queued_at=time.monotonic(),
            channel_id=channel_id,
        )
        return await self._outbox.enqueue(key=key, op=op, wait=True)

    async def _enqueue_edit(
        self,
        *,
        channel_id: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None,
        wait: bool,
    ) -> SlackMessage | None:
        key = self._edit_key(channel_id, ts)
        op = OutboxOp(
            execute=lambda: self._client.update_message(
                channel_id=channel_id,
                ts=ts,
                text=text,
                blocks=blocks,
            ),
            priority=EDIT_PRIORITY,
            queued_at=time.monotonic(),
            channel_id=channel_id,
        )
        return await self._outbox.enqueue(key=key, op=op, wait=wait)

    async def _enqueue_delete(self, *, channel_id: str, ts: str) -> bool:
        edit_key = self._edit_key(channel_id, ts)
        await self._outbox.drop_pending(key=edit_key)
        delete_key = self._delete_key(channel_id, ts)
        op = OutboxOp(
            execute=lambda: self._client.delete_message(
                channel_id=channel_id,
                ts=ts,
            ),
            priority=DELETE_PRIORITY,
            queued_at=time.monotonic(),
            channel_id=channel_id,
        )
        result = await self._outbox.enqueue(key=delete_key, op=op, wait=True)
        return bool(result)


def _is_cancelled_label(label: str) -> bool:
    stripped = label.strip()
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) >= 2:
        stripped = stripped[1:-1]
    return stripped.lower() == "cancelled"


def _format_elapsed(elapsed_s: float) -> str:
    total = max(0, int(elapsed_s))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _format_header(
    elapsed_s: float, step: int | None, *, label: str, engine: str
) -> str:
    elapsed = _format_elapsed(elapsed_s)
    parts = [label, engine, elapsed]
    if step is not None:
        parts.append(f"step {step}")
    return " · ".join(parts)


def _shorten(text: str, width: int | None) -> str:
    if width is None:
        return text
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3]}..."


def _format_action_title(action) -> str:
    title = str(action.title or "").strip()
    if not title:
        title = action.kind
    if action.kind == "command":
        return f"`{_shorten(title, 160)}`"
    if action.kind == "tool":
        return f"tool: {_shorten(title, 160)}"
    if action.kind == "file_change":
        return f"files: {_shorten(title, 160)}"
    if action.kind in {"note", "warning"}:
        return _shorten(title, 200)
    return _shorten(title, 160)


def _action_status(action_state) -> str:
    if action_state.completed:
        if action_state.ok is False:
            return "err"
        return "ok"
    if action_state.display_phase == "updated":
        return "upd"
    return "run"


def _format_action_line(action_state) -> str:
    status = _action_status(action_state)
    title = _format_action_title(action_state.action)
    return f"[{status}] {title}"


def _format_actions(actions, *, max_actions: int) -> str | None:
    if not actions:
        return None
    if max_actions <= 0:
        return None
    visible = actions[-max_actions:]
    return "\n".join(_format_action_line(item) for item in visible)


def _format_footer(state) -> str | None:
    lines: list[str] = []
    if state.context_line:
        lines.append(state.context_line)
    if state.resume_line:
        lines.append(state.resume_line)
    if not lines:
        return None
    return "\n".join(lines)


def _assemble_sections(*chunks: str | None) -> str:
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _render_progress_text(
    state,
    *,
    elapsed_s: float,
    label: str,
    max_actions: int,
) -> str:
    step = state.action_count or None
    header = _format_header(elapsed_s, step, label=label, engine=state.engine)
    body = _format_actions(state.actions, max_actions=max_actions)
    footer = _format_footer(state)
    return _assemble_sections(header, body, footer)


def _render_final_text(
    state,
    *,
    elapsed_s: float,
    status: str,
    answer: str,
) -> str:
    step = state.action_count or None
    header = _format_header(elapsed_s, step, label=status, engine=state.engine)
    body = (answer or "").strip() or None
    footer = _format_footer(state)
    return _assemble_sections(header, body, footer)


def _trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


def _split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        return [text]
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + max_chars])
        start += max_chars
    return chunks


def _trim_block_text(text: str) -> str:
    if len(text) <= MAX_BLOCK_TEXT:
        return text
    if MAX_BLOCK_TEXT <= 3:
        return text[:MAX_BLOCK_TEXT]
    return f"{text[: MAX_BLOCK_TEXT - 3]}..."


def _split_block_text(text: str) -> list[str]:
    if len(text) <= MAX_BLOCK_TEXT:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + MAX_BLOCK_TEXT])
        start += MAX_BLOCK_TEXT
    return chunks


def _build_cancel_blocks(text: str) -> list[dict[str, Any]]:
    body = _trim_block_text(text)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "cancel"},
                    "action_id": CANCEL_ACTION_ID,
                    "style": "danger",
                    "value": "cancel",
                }
            ],
        },
    ]


def _format_hours_label(hours: float) -> str:
    if hours.is_integer():
        return f"{int(hours)}h"
    return f"{hours:g}h"


def _build_archive_blocks(
    text: str,
    *,
    thread_id: str | None,
    action_blocks: list[dict[str, Any]] | None = None,
    include_actions: bool = True,
) -> list[dict[str, Any]]:
    sections = _split_block_text(text)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in sections
    ]
    if not include_actions:
        return blocks
    if action_blocks is not None:
        blocks.extend(copy.deepcopy(action_blocks))
        return blocks
    return blocks


def _build_archive_confirm_blocks(
    text: str,
    *,
    thread_id: str | None,
) -> list[dict[str, Any]]:
    value = thread_id or ""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "confirm archive"},
                    "action_id": CONFIRM_ARCHIVE_ACTION_ID,
                    "style": "danger",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "cancel"},
                    "action_id": CANCEL_ARCHIVE_ACTION_ID,
                    "value": value,
                },
            ],
        },
    ]


def _build_approval_blocks(
    text: str,
    *,
    thread_id: str | None,
    include_actions: bool = True,
) -> list[dict[str, Any]]:
    value = thread_id or ""
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in _split_block_text(text)
    ]
    if not include_actions:
        return blocks
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "approve once"},
                    "action_id": APPROVE_REQUEST_ACTION_ID,
                    "style": "primary",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "deny"},
                    "action_id": DENY_REQUEST_ACTION_ID,
                    "style": "danger",
                    "value": value,
                },
            ],
        }
    )
    return blocks


def _is_approval_expired(
    approval: PendingApprovalSnapshot,
    *,
    now: float,
) -> bool:
    if approval.status != "pending":
        return False
    if approval.created_at is None:
        return False
    return (now - approval.created_at) > APPROVAL_TTL_S


def _format_approval_request_text(
    cfg: SlackBridgeConfig,
    approval: PendingApprovalSnapshot,
) -> str:
    approvers = " ".join(f"<@{user_id}>" for user_id in _slack_state(cfg).allowed_user_ids)
    requester = (
        f"<@{approval.requester_user_id}>"
        if approval.requester_user_id
        else "an unknown user"
    )
    excerpt = (approval.cleaned_text or "").strip()
    if excerpt:
        excerpt = _trim_text(excerpt, 220)
    elif approval.files:
        count = len(approval.files)
        noun = "file" if count == 1 else "files"
        excerpt = f"{count} attached {noun}"
    text = f"{approvers} approval requested by {requester} to run Takopi."
    if excerpt:
        text = f"{text}\nPrompt: {excerpt}"
    return text


def _format_approval_resolution_text(
    approval: PendingApprovalSnapshot,
    *,
    status: str,
) -> str:
    requester = (
        f"<@{approval.requester_user_id}>"
        if approval.requester_user_id
        else "the requester"
    )
    actor = (
        f"<@{approval.decided_by_user_id}>"
        if approval.decided_by_user_id
        else "an allowed user"
    )
    if status == "approved":
        return f"approval granted by {actor} for {requester}. running request."
    if status == "denied":
        return f"approval denied by {actor} for {requester}."
    return f"approval expired for {requester}."


def _mention_regex(bot_user_id: str) -> re.Pattern[str]:
    escaped = re.escape(bot_user_id)
    return re.compile(rf"<@{escaped}(\|[^>]+)?>")


_BOT_TOKEN_STRIP = "*_~`"


def _normalize_bot_token(token: str) -> str:
    trimmed = token.strip().strip(_BOT_TOKEN_STRIP).strip()
    if trimmed.startswith("@"):
        trimmed = trimmed[1:]
    trimmed = trimmed.strip(_BOT_TOKEN_STRIP).strip(".,:;")
    return trimmed.lower()


def _normalize_command_id(command_id: str) -> str:
    normalized = command_id.strip().lower()
    for prefix in ("takopi-", "takopi_"):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized


def _normalize_route_key(value: str) -> str:
    return " ".join(_normalize_command_id(value).split())


def _strip_bot_name(text: str, *, bot_name: str) -> str:
    tokens = text.split()
    if not tokens:
        return text
    target = bot_name.lower()
    while tokens:
        if _normalize_bot_token(tokens[0]) == target:
            tokens.pop(0)
            continue
        if _normalize_bot_token(tokens[-1]) == target:
            tokens.pop()
            continue
        break
    return " ".join(tokens).strip()


def _strip_bot_mention(
    text: str,
    *,
    bot_user_id: str | None,
    bot_name: str | None,
) -> str:
    cleaned = text
    if bot_user_id is not None:
        pattern = _mention_regex(bot_user_id)
        cleaned = pattern.sub("", text)
    if bot_name:
        cleaned = _strip_bot_name(cleaned, bot_name=bot_name)
    return cleaned.strip()


def _parse_form_payload(raw: str) -> dict[str, str]:
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _coerce_socket_payload(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        raw = payload.strip()
        if raw.startswith("{") and raw.endswith("}"):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                return value
        parsed = _parse_form_payload(raw)
        if "payload" in parsed:
            try:
                decoded = json.loads(parsed["payload"])
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                return decoded
        return parsed
    return None


def _should_skip_message(message: SlackMessage, bot_user_id: str | None) -> bool:
    if not message.ts:
        return True
    if message.subtype is not None and message.subtype != "file_share":
        return True
    if message.bot_id is not None:
        return True
    if message.user is None:
        return True
    if bot_user_id is not None and message.user == bot_user_id:
        return True
    if not message.text or not message.text.strip():
        return not bool(message.files)
    return False


def _is_allowed_channel(cfg: SlackBridgeConfig, channel_id: str | None) -> bool:
    state = _slack_state(cfg)
    if not isinstance(channel_id, str) or not channel_id:
        return False
    if "*" in state.allowed_channel_ids:
        return True
    return channel_id in state.allowed_channel_ids


def _is_allowed_user(cfg: SlackBridgeConfig, user_id: str | None) -> bool:
    state = _slack_state(cfg)
    if not state.allowed_user_ids:
        return True
    if not isinstance(user_id, str) or not user_id:
        return False
    return user_id in state.allowed_user_ids


def _should_process_socket_message(
    event: dict[str, Any],
    message: SlackMessage,
) -> bool:
    event_type = event.get("type")
    if event_type == "app_mention":
        return True
    channel_type = event.get("channel_type")
    if channel_type in {"im", "mpim"}:
        return True
    return message.thread_ts is not None


def _response_thread_id_for_message(
    cfg: SlackBridgeConfig,
    message: SlackMessage,
) -> str | None:
    if _slack_state(cfg).reply_mode == "channel":
        return None
    return message.thread_ts or message.ts


def _response_thread_id(
    cfg: SlackBridgeConfig,
    *,
    thread_ts: str | None,
) -> str | None:
    if _slack_state(cfg).reply_mode == "channel":
        return None
    return thread_ts


async def _publish_approval_message(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    thread_id: str,
    approval: PendingApprovalSnapshot,
    text: str,
    include_actions: bool,
) -> None:
    blocks = _build_approval_blocks(
        text,
        thread_id=thread_id,
        include_actions=include_actions,
    )
    if approval.approval_message_ts:
        await cfg.client.update_message(
            channel_id=channel_id,
            ts=approval.approval_message_ts,
            text=text,
            blocks=blocks,
        )
        return
    sent = await cfg.client.post_message(
        channel_id=channel_id,
        text=text,
        blocks=blocks,
        thread_ts=approval.response_thread_id,
    )
    if cfg.thread_store is not None:
        await cfg.thread_store.set_pending_approval_message(
            channel_id=channel_id,
            thread_id=thread_id,
            approval_message_ts=sent.ts,
        )


async def _request_approval_for_message(
    cfg: SlackBridgeConfig,
    *,
    message: SlackMessage,
    text: str,
) -> bool:
    thread_store = cfg.thread_store
    channel_id = message.channel_id or cfg.channel_id
    if thread_store is None or not message.ts or message.thread_ts is not None:
        return False

    thread_id = message.ts
    pending = await thread_store.get_pending_approval(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    now = time.time()
    if pending is not None and _is_approval_expired(pending, now=now):
        pending = await thread_store.resolve_pending_approval(
            channel_id=channel_id,
            thread_id=thread_id,
            status="expired",
            decided_by_user_id=None,
            decided_at=now,
        )
        if pending is not None:
            await _publish_approval_message(
                cfg,
                channel_id=channel_id,
                thread_id=thread_id,
                approval=pending,
                text=_format_approval_resolution_text(pending, status="expired"),
                include_actions=False,
            )
        pending = None
    if pending is not None and pending.status == "pending" and pending.approval_message_ts:
        return True

    if pending is None or pending.status != "pending":
        pending = await thread_store.set_pending_approval(
            channel_id=channel_id,
            thread_id=thread_id,
            requester_user_id=message.user,
            source_message_ts=message.ts,
            response_thread_id=_response_thread_id_for_message(cfg, message),
            cleaned_text=text or None,
            created_at=now,
            files=[dict(item) for item in message.files],
        )
    await _publish_approval_message(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
        approval=pending,
        text=_format_approval_request_text(cfg, pending),
        include_actions=True,
    )
    return True


async def _send_startup(cfg: SlackBridgeConfig) -> None:
    startup_msg = _slack_state(cfg).startup_msg
    if not startup_msg.strip():
        return
    message = RenderedMessage(text=startup_msg)
    sent = await cfg.exec_cfg.transport.send(
        channel_id=cfg.channel_id,
        message=message,
    )
    if sent is not None:
        logger.info("startup.sent", channel_id=cfg.channel_id)


async def _handle_slack_message(
    cfg: SlackBridgeConfig,
    message: SlackMessage,
    text: str,
    running_tasks: RunningTasks,
) -> None:
    channel_id = message.channel_id or cfg.channel_id
    is_thread_reply = message.thread_ts is not None
    session_thread_id = message.thread_ts or message.ts
    response_thread_id = _response_thread_id_for_message(cfg, message)
    thread_store = cfg.thread_store
    try:
        # Reuse Takopi's directive parser to avoid double parsing.
        directives = parse_directives(
            text,
            engine_ids=cfg.runtime.engine_ids,
            projects=cfg.runtime._projects,
        )
    except DirectiveError as exc:
        await send_plain(
            cfg.exec_cfg,
            channel_id=channel_id,
            user_msg_id=message.ts,
            thread_id=response_thread_id,
            text=f"error:\n{exc}",
            notify=False,
        )
        return

    context: RunContext | None = None
    engine_override = directives.engine
    prompt = directives.prompt
    if directives.project is not None:
        context = RunContext(project=directives.project, branch=directives.branch)
        if thread_store is not None and session_thread_id is not None:
            await thread_store.set_context(
                channel_id=channel_id,
                thread_id=session_thread_id,
                context=context,
            )
            if engine_override is None:
                engine_override = await thread_store.get_default_engine(
                    channel_id=channel_id,
                    thread_id=session_thread_id,
                )
    elif is_thread_reply and thread_store is not None and session_thread_id is not None:
        context = await thread_store.get_context(
            channel_id=channel_id,
            thread_id=session_thread_id,
        )
        if context is not None:
            if directives.branch is not None and context.project is not None:
                context = RunContext(project=context.project, branch=directives.branch)
                await thread_store.set_context(
                    channel_id=channel_id,
                    thread_id=session_thread_id,
                    context=context,
                )
            if engine_override is None:
                engine_override = await thread_store.get_default_engine(
                    channel_id=channel_id,
                    thread_id=session_thread_id,
                )

    if thread_store is not None and session_thread_id is not None:
        worktree = None
        if context is not None and context.project and context.branch:
            worktree = WorktreeSnapshot(
                project=context.project,
                branch=context.branch,
            )
        clear_worktree = (
            context is not None and context.project is not None and not context.branch
        )
        await thread_store.record_activity(
            channel_id=channel_id,
            thread_id=session_thread_id,
            user_id=message.user,
            worktree=worktree,
            clear_worktree=clear_worktree,
            now=time.time(),
        )

    if directives.project is None and directives.branch is not None and context is None:
        prompt = f"@{directives.branch} {prompt}".strip()

    prompt = await _resolve_prompt_from_media(
        cfg,
        message=message,
        prompt=prompt,
        context=context,
        thread_id=response_thread_id,
    )
    if prompt is None:
        return

    inline_command = None
    if "/" in prompt:
        allowed_commands = set(
            list_ids(
                COMMAND_GROUP,
                allowlist=cfg.runtime.allowlist,
                reserved_ids=RESERVED_COMMAND_IDS,
            )
        )
        inline_command = _extract_inline_command(
            prompt, allowed_commands=allowed_commands
        )
    if inline_command:
        command_id, args_text, command_text = inline_command
        command_context = None
        if thread_store is not None:
            command_context = await _resolve_command_context(
                cfg,
                channel_id=channel_id,
                thread_id=session_thread_id,
            )
        default_context = context
        if default_context is None and command_context is not None:
            default_context = command_context.default_context
        default_engine_override = engine_override
        if default_engine_override is None and command_context is not None:
            default_engine_override = command_context.default_engine_override

        reply_ref = MessageRef(
            channel_id=channel_id,
            message_id=message.ts,
            thread_id=response_thread_id,
        )
        full_text = command_text
        context_prefix = _format_context_directive(default_context)
        if context_prefix is not None:
            full_text = f"{context_prefix} {command_text}"
        handled = await _dispatch_with_command_context(
            cfg,
            command_id=command_id,
            args_text=args_text,
            full_text=full_text,
            channel_id=channel_id,
            message_id=message.ts,
            thread_id=response_thread_id,
            reply_ref=reply_ref,
            reply_text=None,
            running_tasks=running_tasks,
            command_context=command_context,
            default_context=default_context,
            default_engine_override=default_engine_override,
        )
        if handled:
            return

    # Router access avoids re-parsing directives in runtime.resolve_message.
    resume_token = cfg.runtime._router.resolve_resume(prompt, None)
    engine_for_session = cfg.runtime.resolve_engine(
        engine_override=engine_override,
        context=context,
    )
    if thread_store is not None and session_thread_id is not None:
        if resume_token is not None:
            await thread_store.set_resume(
                channel_id=channel_id,
                thread_id=session_thread_id,
                token=resume_token,
            )
        else:
            resume_token = await thread_store.get_resume(
                channel_id=channel_id,
                thread_id=session_thread_id,
                engine=engine_for_session,
            )
    run_options = await _resolve_run_options(
        thread_store,
        channel_id=channel_id,
        thread_id=session_thread_id,
        engine_id=engine_for_session,
    )
    on_thread_known = _make_resume_saver(
        thread_store,
        channel_id=channel_id,
        thread_id=session_thread_id,
    )

    await run_engine(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=running_tasks,
        channel_id=channel_id,
        user_msg_id=message.ts,
        text=prompt,
        resume_token=resume_token,
        context=context,
        engine_override=engine_for_session,
        thread_id=response_thread_id,
        on_thread_known=on_thread_known,
        run_options=run_options,
    )


async def _resolve_prompt_from_media(
    cfg: SlackBridgeConfig,
    *,
    message: SlackMessage,
    prompt: str,
    context: RunContext | None,
    thread_id: str | None,
) -> str | None:
    channel_id = message.channel_id or cfg.channel_id
    files = extract_files(message.files)

    if prompt.strip():
        tokens = split_command_args(prompt)
        if tokens and tokens[0].lstrip("/").lower() == "file":
            args_text = prompt[len(tokens[0]) :].strip()
            await handle_file_command(
                cfg,
                channel_id=channel_id,
                message_ts=message.ts,
                thread_ts=thread_id,
                user_id=message.user,
                args_text=args_text,
                files=files,
                ambient_context=context,
            )
            return None

    if files:
        prompt = await handle_file_uploads(
            cfg,
            channel_id=channel_id,
            message_ts=message.ts,
            thread_ts=thread_id,
            user_id=message.user,
            caption_text=prompt,
            files=files,
            ambient_context=context,
        )
        return prompt

    if not prompt.strip():
        return None
    return prompt


async def _safe_handle_slack_message(
    cfg: SlackBridgeConfig,
    message: SlackMessage,
    text: str,
    running_tasks: RunningTasks,
) -> None:
    try:
        await _handle_slack_message(cfg, message, text, running_tasks)
    except Exception as exc:
        logger.exception(
            "slack.message_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


def _session_thread_id(channel_id: str, thread_ts: str | None) -> str:
    return thread_ts if thread_ts else channel_id


def _resolve_command_channel(
    cfg: SlackBridgeConfig,
    *,
    command_id: str,
    args_text: str = "",
    source_channel_id: str | None = None,
) -> str:
    state = _slack_state(cfg)
    command_key = _normalize_route_key(command_id)
    if args_text:
        tokens = split_command_args(args_text)
        if tokens:
            subcommand_key = _normalize_route_key(f"{command_key} {tokens[0]}")
            if subcommand_key in state.plugin_channels:
                return state.plugin_channels[subcommand_key]
    fallback_channel_id = source_channel_id or cfg.channel_id
    return state.plugin_channels.get(command_key, fallback_channel_id)


async def _respond_ephemeral(
    cfg: SlackBridgeConfig,
    *,
    response_url: str | None,
    channel_id: str,
    text: str,
    replace_original: bool | None = None,
    delete_original: bool | None = None,
) -> None:
    if response_url:
        await cfg.client.post_response(
            response_url=response_url,
            text=text,
            response_type="ephemeral",
            replace_original=replace_original,
            delete_original=delete_original,
        )
        return
    await cfg.client.post_message(channel_id=channel_id, text=text)


def _extract_command_text(tokens: tuple[str, ...], raw_text: str) -> tuple[str, str]:
    head = tokens[0]
    command_id = head.lstrip("/").lower()
    args_text = raw_text[len(head) :].strip()
    return command_id, args_text


def _format_context_directive(context: RunContext | None) -> str | None:
    if context is None or context.project is None:
        return None
    if context.branch:
        return f"/{context.project} @{context.branch}"
    return f"/{context.project}"


def _extract_inline_command(
    prompt: str, *, allowed_commands: set[str]
) -> tuple[str, str, str] | None:
    if not prompt.strip() or not allowed_commands or "/" not in prompt:
        return None
    for match in INLINE_COMMAND_RE.finditer(prompt):
        command_id = match.group("cmd").lower()
        if command_id not in allowed_commands:
            continue
        command_text = prompt[match.start("token") :].lstrip()
        tokens = split_command_args(command_text)
        if not tokens:
            continue
        parsed_id, args_text = _extract_command_text(tokens, command_text)
        if parsed_id.lower() != command_id:
            continue
        return parsed_id, args_text, command_text
    return None


def _extract_slash_payload_command(command: object) -> str | None:
    if not isinstance(command, str):
        return None
    value = command.strip()
    if not value:
        return None
    if value.startswith("/"):
        value = value[1:]
    lowered = value.lower()
    for prefix in ("takopi-", "takopi_"):
        if lowered.startswith(prefix) and len(lowered) > len(prefix):
            return lowered[len(prefix) :]
    return None


def _parse_thread_ts(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


async def _resolve_run_options(
    thread_store: SlackThreadSessionStore | None,
    *,
    channel_id: str,
    thread_id: str | None,
    engine_id: str,
) -> EngineRunOptions | None:
    if thread_store is None or thread_id is None:
        return None
    model = await thread_store.get_model_override(
        channel_id=channel_id,
        thread_id=thread_id,
        engine=engine_id,
    )
    reasoning = await thread_store.get_reasoning_override(
        channel_id=channel_id,
        thread_id=thread_id,
        engine=engine_id,
    )
    if model or reasoning:
        return EngineRunOptions(model=model, reasoning=reasoning)
    return None


def _make_resume_saver(
    thread_store: SlackThreadSessionStore | None,
    *,
    channel_id: str,
    thread_id: str | None,
):
    if thread_store is None or thread_id is None:
        return None

    async def _note_resume(token, done: anyio.Event) -> None:
        _ = done
        await thread_store.set_resume(
            channel_id=channel_id,
            thread_id=thread_id,
            token=token,
        )

    return _note_resume


async def _resolve_command_context(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    thread_id: str,
) -> CommandContext | None:
    thread_store = cfg.thread_store
    if thread_store is None:
        return None
    default_context = await thread_store.get_context(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    default_engine_override = await thread_store.get_default_engine(
        channel_id=channel_id,
        thread_id=thread_id,
    )

    async def engine_overrides_resolver(
        engine_id: str,
    ) -> EngineRunOptions | None:
        return await _resolve_run_options(
            thread_store,
            channel_id=channel_id,
            thread_id=thread_id,
            engine_id=engine_id,
        )
    on_thread_known = _make_resume_saver(
        thread_store,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    return CommandContext(
        default_context=default_context,
        default_engine_override=default_engine_override,
        engine_overrides_resolver=engine_overrides_resolver,
        on_thread_known=on_thread_known,
    )


async def _dispatch_with_command_context(
    cfg: SlackBridgeConfig,
    *,
    command_id: str,
    args_text: str,
    full_text: str,
    channel_id: str,
    thread_id: str | None,
    message_id: str,
    reply_ref: MessageRef | None,
    reply_text: str | None,
    running_tasks: RunningTasks,
    command_context: CommandContext | None,
    default_context: RunContext | None = None,
    default_engine_override: str | None = None,
) -> bool:
    resolved_default_context = default_context
    resolved_default_engine_override = default_engine_override
    if command_context is not None:
        if resolved_default_context is None:
            resolved_default_context = command_context.default_context
        if resolved_default_engine_override is None:
            resolved_default_engine_override = command_context.default_engine_override

    return await dispatch_command(
        cfg,
        command_id=command_id,
        args_text=args_text,
        full_text=full_text,
        channel_id=channel_id,
        output_channel_id=_resolve_command_channel(
            cfg,
            command_id=command_id,
            args_text=args_text,
            source_channel_id=channel_id,
        ),
        message_id=message_id,
        thread_id=thread_id,
        reply_ref=reply_ref,
        reply_text=reply_text,
        running_tasks=running_tasks,
        on_thread_known=command_context.on_thread_known
        if command_context is not None
        else None,
        default_engine_override=resolved_default_engine_override,
        default_context=resolved_default_context,
        engine_overrides_resolver=command_context.engine_overrides_resolver
        if command_context is not None
        else None,
    )


async def _respond_to_slash(
    cfg: SlackBridgeConfig,
    request: SlashCommandRequest,
    text: str,
) -> None:
    await _respond_ephemeral(
        cfg,
        response_url=request.response_url,
        channel_id=request.channel_id,
        text=text,
    )


async def _parse_slash_command_request(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
) -> SlashCommandRequest | None:
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not _is_allowed_channel(cfg, channel_id):
        return None
    user_id = payload.get("user_id")
    if not isinstance(user_id, str):
        user_id = None
    response_url = payload.get("response_url")
    if not isinstance(response_url, str) or not response_url:
        response_url = None
    if not _is_allowed_user(cfg, user_id):
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text="this Slack user is not allowed to use Takopi.",
        )
        return None

    text = payload.get("text") or ""
    if not isinstance(text, str):
        text = ""
    thread_ts = _parse_thread_ts(payload.get("thread_ts") or payload.get("message_ts"))
    session_thread_id = _session_thread_id(channel_id, thread_ts)
    response_thread_id = _response_thread_id(cfg, thread_ts=thread_ts)

    command_id = _extract_slash_payload_command(payload.get("command"))
    if command_id:
        args_text = text.strip()
        tokens = (command_id, *split_command_args(args_text))
    else:
        tokens = split_command_args(text)
        if not tokens:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=_slash_usage(),
            )
            return None
        command_id, args_text = _extract_command_text(tokens, text)

    return SlashCommandRequest(
        channel_id=channel_id,
        user_id=user_id,
        response_url=response_url,
        session_thread_id=session_thread_id,
        response_thread_id=response_thread_id,
        command_id=command_id,
        args_text=args_text,
        tokens=tokens,
    )


async def _handle_slash_file_command(
    cfg: SlackBridgeConfig,
    request: SlashCommandRequest,
) -> None:
    command_context = None
    if cfg.thread_store is not None:
        command_context = await _resolve_command_context(
            cfg,
            channel_id=request.channel_id,
            thread_id=request.session_thread_id,
        )
    await handle_file_command(
        cfg,
        channel_id=request.channel_id,
        message_ts=None,
        thread_ts=request.response_thread_id,
        user_id=request.user_id,
        args_text=request.args_text,
        files=[],
        ambient_context=command_context.default_context
        if command_context is not None
        else None,
    )


async def _maybe_handle_slash_builtin(
    cfg: SlackBridgeConfig,
    request: SlashCommandRequest,
) -> bool:
    thread_store = cfg.thread_store
    if thread_store is None and request.command_id in {
        "status",
        "engine",
        "model",
        "reasoning",
        "session",
    }:
        await _respond_to_slash(
            cfg,
            request,
            "Slack thread state store is not configured.",
        )
        return True
    if thread_store is None:
        return False

    if request.command_id == "status":
        state = await thread_store.get_state(
            channel_id=request.channel_id,
            thread_id=request.session_thread_id,
        )
        await _respond_to_slash(cfg, request, _format_status(state))
        return True

    if request.command_id == "engine":
        if len(request.tokens) < 2:
            await _respond_to_slash(cfg, request, "usage: /takopi engine <engine|clear>")
            return True
        engine_value = request.tokens[1].strip()
        if engine_value.lower() == "clear":
            await thread_store.set_default_engine(
                channel_id=request.channel_id,
                thread_id=request.session_thread_id,
                engine=None,
            )
            await _respond_to_slash(
                cfg,
                request,
                "default engine cleared for this thread.",
            )
            return True
        engine_id = engine_value.lower()
        if engine_id not in cfg.runtime.engine_ids:
            await _respond_to_slash(cfg, request, f"unknown engine: `{engine_value}`")
            return True
        await thread_store.set_default_engine(
            channel_id=request.channel_id,
            thread_id=request.session_thread_id,
            engine=engine_id,
        )
        await _respond_to_slash(
            cfg,
            request,
            f"default engine set to `{engine_id}` for this thread.",
        )
        return True

    if request.command_id == "model":
        if len(request.tokens) < 3:
            await _respond_to_slash(
                cfg,
                request,
                "usage: /takopi model <engine> <model|clear>",
            )
            return True
        engine_id = request.tokens[1].strip().lower()
        model = request.tokens[2].strip()
        if engine_id not in cfg.runtime.engine_ids:
            await _respond_to_slash(cfg, request, f"unknown engine: `{engine_id}`")
            return True
        value = None if model.lower() == "clear" else model
        await thread_store.set_model_override(
            channel_id=request.channel_id,
            thread_id=request.session_thread_id,
            engine=engine_id,
            model=value,
        )
        status = "cleared" if value is None else f"set to `{value}`"
        await _respond_to_slash(
            cfg,
            request,
            f"model override {status} for `{engine_id}`.",
        )
        return True

    if request.command_id == "reasoning":
        if len(request.tokens) < 3:
            await _respond_to_slash(
                cfg,
                request,
                "usage: /takopi reasoning <engine> <level|clear>",
            )
            return True
        engine_id = request.tokens[1].strip().lower()
        level = request.tokens[2].strip().lower()
        if engine_id not in cfg.runtime.engine_ids:
            await _respond_to_slash(cfg, request, f"unknown engine: `{engine_id}`")
            return True
        if level == "clear":
            await thread_store.set_reasoning_override(
                channel_id=request.channel_id,
                thread_id=request.session_thread_id,
                engine=engine_id,
                level=None,
            )
            await _respond_to_slash(
                cfg,
                request,
                f"reasoning override cleared for `{engine_id}`.",
            )
            return True
        if not is_valid_reasoning_level(level):
            valid = ", ".join(sorted(REASONING_LEVELS))
            await _respond_to_slash(
                cfg,
                request,
                f"invalid reasoning level. valid: {valid}",
            )
            return True
        if not supports_reasoning(engine_id):
            await _respond_to_slash(
                cfg,
                request,
                f"engine `{engine_id}` does not support reasoning overrides.",
            )
            return True
        await thread_store.set_reasoning_override(
            channel_id=request.channel_id,
            thread_id=request.session_thread_id,
            engine=engine_id,
            level=level,
        )
        await _respond_to_slash(
            cfg,
            request,
            f"reasoning override set to `{level}` for `{engine_id}`.",
        )
        return True

    if (
        request.command_id == "session"
        and len(request.tokens) >= 2
        and request.tokens[1].lower() == "clear"
    ):
        await thread_store.clear_resumes(
            channel_id=request.channel_id,
            thread_id=request.session_thread_id,
        )
        await _respond_to_slash(
            cfg,
            request,
            "resume tokens cleared for this thread.",
        )
        return True

    return False


async def _handle_slash_command(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    request = await _parse_slash_command_request(cfg, payload)
    if request is None:
        return
    if request.command_id in {"help", "usage"}:
        await _respond_to_slash(cfg, request, _slash_usage())
        return
    if request.command_id == "file":
        await _handle_slash_file_command(cfg, request)
        return
    if await _maybe_handle_slash_builtin(cfg, request):
        return

    command_context = await _resolve_command_context(
        cfg,
        channel_id=request.channel_id,
        thread_id=request.session_thread_id,
    )
    if command_context is None:
        return

    full_text = f"/{request.command_id} {request.args_text}".strip()
    context_prefix = _format_context_directive(command_context.default_context)
    if context_prefix is not None:
        full_text = f"{context_prefix} {full_text}"
    handled = await _dispatch_with_command_context(
        cfg,
        command_id=request.command_id,
        args_text=request.args_text,
        full_text=full_text,
        channel_id=request.channel_id,
        message_id="0",
        thread_id=request.response_thread_id,
        reply_ref=None,
        reply_text=None,
        running_tasks=running_tasks,
        command_context=command_context,
    )
    if not handled:
        await _respond_to_slash(cfg, request, f"unknown command `{request.command_id}`.")


async def _handle_interactive(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    channel_id = _extract_payload_channel_id(payload)
    if channel_id is not None and not _is_allowed_channel(cfg, channel_id):
        return
    user_id = _extract_payload_user_id(payload)
    if channel_id is not None and not _is_allowed_user(cfg, user_id):
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="this Slack user is not allowed to use Takopi.",
        )
        return
    payload_type = payload.get("type")
    if payload_type == "block_actions":
        if await _handle_approval_approve_action(cfg, payload, running_tasks):
            return
        if await _handle_approval_deny_action(cfg, payload):
            return
        if await _handle_archive_confirm_action(cfg, payload):
            return
        if await _handle_archive_cancel_action(cfg, payload):
            return
        if await _handle_archive_action(cfg, payload):
            return
        if await _handle_custom_action(cfg, payload, running_tasks):
            return
        await _handle_cancel_action(cfg, payload, running_tasks)
        return
    if payload_type in {"message_action", "shortcut"}:
        await _handle_shortcut(cfg, payload, running_tasks)


def _extract_block_action(
    actions: object,
    *,
    action_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(actions, list):
        return None
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_id = action.get("action_id")
        if isinstance(action_id, str) and action_id in action_ids:
            return action
    return None


def _extract_response_url(payload: dict[str, Any]) -> str | None:
    response_url = payload.get("response_url")
    if isinstance(response_url, str) and response_url:
        return response_url
    return None


def _extract_payload_channel_id(payload: dict[str, Any]) -> str | None:
    channel = payload.get("channel")
    if isinstance(channel, dict):
        channel_id = channel.get("id")
        if isinstance(channel_id, str) and channel_id:
            return channel_id
    channel_id = payload.get("channel_id")
    if isinstance(channel_id, str) and channel_id:
        return channel_id
    return None


def _extract_payload_user_id(payload: dict[str, Any]) -> str | None:
    user = payload.get("user")
    if isinstance(user, dict):
        user_id = user.get("id")
        if isinstance(user_id, str) and user_id:
            return user_id
    user_id = payload.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id
    return None


def _extract_action_thread_id(
    payload: dict[str, Any],
    action: dict[str, Any],
) -> str | None:
    value = action.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    message = payload.get("message")
    if isinstance(message, dict):
        thread_ts = _parse_thread_ts(message.get("thread_ts"))
        if thread_ts:
            return thread_ts
        ts = _parse_thread_ts(message.get("ts"))
        if ts:
            return ts
    container = payload.get("container")
    if isinstance(container, dict):
        thread_ts = _parse_thread_ts(container.get("thread_ts"))
        if thread_ts:
            return thread_ts
        ts = _parse_thread_ts(container.get("message_ts"))
        if ts:
            return ts
    return None


def _extract_payload_thread_id(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, dict):
        thread_ts = _parse_thread_ts(message.get("thread_ts"))
        if thread_ts:
            return thread_ts
        ts = _parse_thread_ts(message.get("ts"))
        if ts:
            return ts
    container = payload.get("container")
    if isinstance(container, dict):
        thread_ts = _parse_thread_ts(container.get("thread_ts"))
        if thread_ts:
            return thread_ts
        ts = _parse_thread_ts(container.get("message_ts"))
        if ts:
            return ts
    return None


def _extract_action_message_ts(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, dict):
        ts = _parse_thread_ts(message.get("ts"))
        if ts:
            return ts
    container = payload.get("container")
    if isinstance(container, dict):
        ts = _parse_thread_ts(container.get("message_ts"))
        if ts:
            return ts
    return None


async def _send_archive_message(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    thread_id: str | None,
    text: str,
    include_actions: bool,
) -> None:
    blocks = _build_archive_blocks(
        text,
        thread_id=thread_id,
        action_blocks=cfg.state.action_blocks,
        include_actions=include_actions,
    )
    await cfg.client.post_message(
        channel_id=channel_id,
        text=text,
        blocks=blocks,
        thread_ts=thread_id,
    )


async def _clear_archive_actions(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    message_ts: str | None,
    thread_id: str | None,
    text: str | None,
) -> None:
    if not message_ts or not isinstance(text, str) or not text:
        return
    await cfg.client.update_message(
        channel_id=channel_id,
        ts=message_ts,
        text=text,
        blocks=_build_archive_blocks(
            text,
            thread_id=thread_id,
            action_blocks=cfg.state.action_blocks,
            include_actions=False,
        ),
    )


async def _reset_project_to_origin_main(
    cfg: SlackBridgeConfig,
    *,
    project: str,
) -> tuple[bool, str]:
    try:
        base_path = cfg.runtime.resolve_run_cwd(
            RunContext(project=project, branch=None)
        )
    except ConfigError as exc:
        return False, f"could not resolve project path: {exc}"
    path = _safely_resolve_path(base_path)
    if path is None or not path.exists():
        return False, "project path not found on disk."

    code, stdout, stderr = await _run_git(
        ["git", "-C", str(path), "fetch", "origin", "main"],
        cwd=path,
    )
    if code != 0:
        details = stderr.strip() or stdout.strip()
        return False, f"git fetch failed: {details}"

    code, stdout, stderr = await _run_git(
        ["git", "-C", str(path), "reset", "--hard", "origin/main"],
        cwd=path,
    )
    if code != 0:
        details = stderr.strip() or stdout.strip()
        return False, f"git reset failed: {details}"

    code, stdout, stderr = await _run_git(
        ["git", "-C", str(path), "clean", "-fd"],
        cwd=path,
    )
    if code != 0:
        details = stderr.strip() or stdout.strip()
        return False, f"git clean failed: {details}"

    return True, "project reset to origin/main."


async def _handle_archive_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
) -> bool:
    actions = payload.get("actions")
    action = _extract_block_action(actions, action_ids={ARCHIVE_ACTION_ID})
    if action is None:
        return False
    if cfg.thread_store is None:
        return True
    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(channel_id, str):
        return True
    thread_id = _extract_action_thread_id(payload, action)
    if thread_id is None:
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="missing thread id for archive action.",
        )
        return True
    snapshot = await cfg.thread_store.get_thread_snapshot(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    context = await cfg.thread_store.get_context(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if snapshot is not None and snapshot.worktree is not None:
        label = _format_worktree_ref(snapshot.worktree)
    elif context is not None and context.project:
        label = f"`/{context.project}`"
    else:
        label = "this thread"

    prompt = (
        f"Archive {label}? This will delete the worktree and discard uncommitted changes."
    )
    await cfg.client.post_message(
        channel_id=channel_id,
        text=prompt,
        blocks=_build_archive_confirm_blocks(prompt, thread_id=thread_id),
        thread_ts=thread_id,
    )
    return True


async def _handle_archive_confirm_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
) -> bool:
    actions = payload.get("actions")
    action = _extract_block_action(actions, action_ids={CONFIRM_ARCHIVE_ACTION_ID})
    if action is None:
        return False
    if cfg.thread_store is None:
        return True
    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(channel_id, str):
        return True
    thread_id = _extract_action_thread_id(payload, action)
    if thread_id is None:
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="missing thread id for archive confirmation.",
        )
        return True
    message_ts = _extract_action_message_ts(payload)
    await _archive_thread(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
        message_ts=message_ts,
    )
    return True


async def _handle_archive_cancel_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
) -> bool:
    actions = payload.get("actions")
    action = _extract_block_action(actions, action_ids={CANCEL_ARCHIVE_ACTION_ID})
    if action is None:
        return False
    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(channel_id, str):
        return True
    message_ts = _extract_action_message_ts(payload)
    if not isinstance(message_ts, str):
        return True
    text = "archive cancelled."
    await cfg.client.update_message(
        channel_id=channel_id,
        ts=message_ts,
        text=text,
        blocks=_build_archive_blocks(
            text,
            thread_id=_extract_payload_thread_id(payload),
            action_blocks=cfg.state.action_blocks,
            include_actions=False,
        ),
    )
    return True


async def _handle_approval_approve_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> bool:
    actions = payload.get("actions")
    action = _extract_block_action(actions, action_ids={APPROVE_REQUEST_ACTION_ID})
    if action is None:
        return False
    if cfg.thread_store is None:
        return True
    channel_id = _extract_payload_channel_id(payload)
    if not isinstance(channel_id, str):
        return True
    thread_id = _extract_action_thread_id(payload, action)
    if thread_id is None:
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="missing thread id for approval.",
        )
        return True
    approval = await cfg.thread_store.get_pending_approval(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if approval is None:
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="approval request not found.",
        )
        return True
    now = time.time()
    if _is_approval_expired(approval, now=now):
        approval = await cfg.thread_store.resolve_pending_approval(
            channel_id=channel_id,
            thread_id=thread_id,
            status="expired",
            decided_by_user_id=None,
            decided_at=now,
        )
        if approval is not None:
            await _publish_approval_message(
                cfg,
                channel_id=channel_id,
                thread_id=thread_id,
                approval=approval,
                text=_format_approval_resolution_text(approval, status="expired"),
                include_actions=False,
            )
        return True
    if approval.status != "pending":
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text=f"approval already {approval.status}.",
        )
        return True
    approval = await cfg.thread_store.resolve_pending_approval(
        channel_id=channel_id,
        thread_id=thread_id,
        status="approved",
        decided_by_user_id=_extract_payload_user_id(payload),
        decided_at=now,
    )
    if approval is None:
        return True
    await _publish_approval_message(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
        approval=approval,
        text=_format_approval_resolution_text(approval, status="approved"),
        include_actions=False,
    )
    await _safe_handle_slack_message(
        cfg,
        SlackMessage(
            ts=approval.source_message_ts or thread_id,
            text=approval.cleaned_text,
            user=approval.requester_user_id,
            bot_id=None,
            subtype=None,
            thread_ts=None,
            channel_id=channel_id,
            files=[dict(item) for item in approval.files],
        ),
        approval.cleaned_text or "",
        running_tasks,
    )
    return True


async def _handle_approval_deny_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
) -> bool:
    actions = payload.get("actions")
    action = _extract_block_action(actions, action_ids={DENY_REQUEST_ACTION_ID})
    if action is None:
        return False
    if cfg.thread_store is None:
        return True
    channel_id = _extract_payload_channel_id(payload)
    if not isinstance(channel_id, str):
        return True
    thread_id = _extract_action_thread_id(payload, action)
    if thread_id is None:
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="missing thread id for denial.",
        )
        return True
    approval = await cfg.thread_store.get_pending_approval(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if approval is None:
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text="approval request not found.",
        )
        return True
    now = time.time()
    if _is_approval_expired(approval, now=now):
        approval = await cfg.thread_store.resolve_pending_approval(
            channel_id=channel_id,
            thread_id=thread_id,
            status="expired",
            decided_by_user_id=None,
            decided_at=now,
        )
        if approval is not None:
            await _publish_approval_message(
                cfg,
                channel_id=channel_id,
                thread_id=thread_id,
                approval=approval,
                text=_format_approval_resolution_text(approval, status="expired"),
                include_actions=False,
            )
        return True
    if approval.status != "pending":
        await _respond_ephemeral(
            cfg,
            response_url=_extract_response_url(payload),
            channel_id=channel_id,
            text=f"approval already {approval.status}.",
        )
        return True
    approval = await cfg.thread_store.resolve_pending_approval(
        channel_id=channel_id,
        thread_id=thread_id,
        status="denied",
        decided_by_user_id=_extract_payload_user_id(payload),
        decided_at=now,
    )
    if approval is None:
        return True
    await _publish_approval_message(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
        approval=approval,
        text=_format_approval_resolution_text(approval, status="denied"),
        include_actions=False,
    )
    return True


async def _handle_custom_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> bool:
    action_map: dict[str, tuple[str, str]] = {}
    for handler in cfg.state.action_handlers:
        action_map[handler.action_id] = (handler.command, handler.args)
    if not action_map:
        return False
    action = _extract_block_action(
        payload.get("actions"),
        action_ids=set(action_map),
    )
    if action is None:
        return False

    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(channel_id, str):
        return True

    action_id = action.get("action_id")
    if not isinstance(action_id, str):
        return True
    action_entry = action_map.get(action_id)
    if action_entry is None:
        return True
    command_id, args_text = action_entry

    thread_ts = _extract_action_thread_id(payload, action)
    if thread_ts is None:
        thread_ts = _extract_payload_thread_id(payload)
    thread_id = _session_thread_id(channel_id, thread_ts)
    response_thread_id = _response_thread_id(cfg, thread_ts=thread_ts)
    command_context = await _resolve_command_context(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if command_context is None:
        return True

    response_url = payload.get("response_url")
    message = payload.get("message") or {}
    message_text = message.get("text") if isinstance(message, dict) else None
    message_ts = _extract_action_message_ts(payload)
    reply_ref = None
    reply_text = None
    if isinstance(message_ts, str):
        reply_ref = MessageRef(
            channel_id=channel_id,
            message_id=message_ts,
            thread_id=response_thread_id,
        )
        if isinstance(message_text, str):
            reply_text = message_text

    full_text = f"/{command_id} {args_text}".strip()
    handled = await _dispatch_with_command_context(
        cfg,
        command_id=command_id,
        args_text=args_text,
        full_text=full_text,
        channel_id=channel_id,
        message_id=message_ts if isinstance(message_ts, str) else "0",
        thread_id=response_thread_id,
        reply_ref=reply_ref,
        reply_text=reply_text,
        running_tasks=running_tasks,
        command_context=command_context,
    )
    if not handled:
        await _respond_ephemeral(
            cfg,
            response_url=response_url if isinstance(response_url, str) else None,
            channel_id=channel_id,
            text=f"unknown command `{command_id}`.",
        )
    return True


async def _archive_thread(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    thread_id: str,
    message_ts: str | None,
) -> None:
    snapshot = await cfg.thread_store.get_thread_snapshot(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if snapshot is not None and snapshot.worktree is not None:
        ok, result = await _delete_worktree_for_snapshot(
            cfg, snapshot, force_cleanup=True
        )
        if ok:
            await cfg.thread_store.clear_worktree(
                channel_id=channel_id,
                thread_id=thread_id,
            )
        text = (
            f"archive: {_format_worktree_ref(snapshot.worktree)} {result}"
            if ok
            else f"archive failed for {_format_worktree_ref(snapshot.worktree)}: {result}"
        )
        await _finalize_archive_message(
            cfg,
            channel_id=channel_id,
            thread_id=thread_id,
            message_ts=message_ts,
            text=text,
        )
        return

    context = await cfg.thread_store.get_context(
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if context is None or not context.project:
        await _finalize_archive_message(
            cfg,
            channel_id=channel_id,
            thread_id=thread_id,
            message_ts=message_ts,
            text="archive failed: no project context found for this thread.",
        )
        return

    ok, result = await _reset_project_to_origin_main(cfg, project=context.project)
    text = (
        f"archive: `/{context.project}` {result}"
        if ok
        else f"archive failed for `/{context.project}`: {result}"
    )
    await _finalize_archive_message(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
        message_ts=message_ts,
        text=text,
    )


async def _finalize_archive_message(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    thread_id: str,
    message_ts: str | None,
    text: str,
) -> None:
    blocks = _build_archive_blocks(
        text,
        thread_id=thread_id,
        action_blocks=cfg.state.action_blocks,
        include_actions=False,
    )
    if isinstance(message_ts, str):
        await cfg.client.update_message(
            channel_id=channel_id,
            ts=message_ts,
            text=text,
            blocks=blocks,
        )
        return
    await cfg.client.post_message(
        channel_id=channel_id,
        text=text,
        blocks=blocks,
        thread_ts=thread_id,
    )


async def _delete_worktree_for_snapshot(
    cfg: SlackBridgeConfig,
    snapshot: ThreadSnapshot,
    *,
    force_cleanup: bool,
) -> tuple[bool, str]:
    worktree = snapshot.worktree
    if worktree is None:
        return False, "worktree data not found for this thread."
    if worktree.branch.lower() in {"main", "master"}:
        return False, "refusing to delete the main branch worktree."
    try:
        worktree_path = cfg.runtime.resolve_run_cwd(
            RunContext(project=worktree.project, branch=worktree.branch)
        )
    except ConfigError as exc:
        return False, f"could not resolve worktree path: {exc}"
    path = _safely_resolve_path(worktree_path)
    if path is None or not path.exists():
        return False, "worktree path not found on disk."

    base_path = None
    try:
        base_path = cfg.runtime.resolve_run_cwd(
            RunContext(project=worktree.project, branch=None)
        )
    except ConfigError:
        base_path = None
    base = _safely_resolve_path(base_path)
    if base is not None and base.resolve() == path.resolve():
        return False, "refusing to delete the project base worktree."

    code, stdout, stderr = await _run_git(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
    )
    if code != 0:
        details = stderr.strip() or stdout.strip()
        return False, f"could not read worktree branch: {details}"
    branch_name = stdout.strip()
    if branch_name and branch_name != worktree.branch:
        return False, (
            f"worktree is on `{branch_name}`, expected `{worktree.branch}`."
        )

    code, stdout, stderr = await _run_git(
        ["git", "-C", str(path), "status", "--porcelain"],
        cwd=path,
    )
    if code != 0:
        details = stderr.strip() or stdout.strip()
        return False, f"could not check worktree status: {details}"
    if stdout.strip():
        if not force_cleanup:
            return False, "worktree has uncommitted changes; clean it before deleting."
        code, stdout, stderr = await _run_git(
            ["git", "-C", str(path), "reset", "--hard", "HEAD"],
            cwd=path,
        )
        if code != 0:
            details = stderr.strip() or stdout.strip()
            return False, f"git reset failed: {details}"
        code, stdout, stderr = await _run_git(
            ["git", "-C", str(path), "clean", "-fd"],
            cwd=path,
        )
        if code != 0:
            details = stderr.strip() or stdout.strip()
            return False, f"git clean failed: {details}"

    code, stdout, stderr = await _run_git(
        ["git", "-C", str(path), "worktree", "remove", str(path)],
        cwd=path,
    )
    if code != 0:
        details = stderr.strip() or stdout.strip()
        return False, f"git worktree remove failed: {details}"

    if force_cleanup:
        return True, "worktree deleted (local changes discarded)."
    return True, "worktree deleted."


async def _handle_cancel_action(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return
    if not any(
        isinstance(action, dict) and action.get("action_id") == CANCEL_ACTION_ID
        for action in actions
    ):
        return
    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    container = payload.get("container") or {}
    message = payload.get("message") or {}
    message_ts = None
    if isinstance(message, dict):
        message_ts = message.get("ts")
    if not message_ts and isinstance(container, dict):
        message_ts = container.get("message_ts")
    if not isinstance(channel_id, str) or not isinstance(message_ts, str):
        return

    cancelled = _request_cancel(running_tasks, channel_id, message_ts)
    if not cancelled:
        return

    response_url = payload.get("response_url")
    await _respond_ephemeral(
        cfg,
        response_url=response_url if isinstance(response_url, str) else None,
        channel_id=channel_id,
        text="cancellation requested.",
    )
    message_text = None
    if isinstance(message, dict):
        message_text = message.get("text")
    thread_id = _extract_payload_thread_id(payload)
    text = message_text or "cancel requested"
    await cfg.client.update_message(
        channel_id=channel_id,
        ts=message_ts,
        text=text,
        blocks=_build_archive_blocks(
            text,
            thread_id=thread_id,
            action_blocks=cfg.state.action_blocks,
            include_actions=True,
        ),
    )


async def _handle_shortcut(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not _is_allowed_channel(cfg, channel_id):
        return
    message = payload.get("message") or {}
    message_text = message.get("text") if isinstance(message, dict) else None
    message_ts = message.get("ts") if isinstance(message, dict) else None
    thread_ts = _parse_thread_ts(message.get("thread_ts") if isinstance(message, dict) else None)
    response_url = payload.get("response_url")
    if not isinstance(message_text, str) or not message_text.strip():
        await _respond_ephemeral(
            cfg,
            response_url=response_url if isinstance(response_url, str) else None,
            channel_id=channel_id,
            text="shortcut message has no text to process.",
        )
        return

    callback_id = payload.get("callback_id") or payload.get("action_id")
    if not isinstance(callback_id, str) or not callback_id.startswith("takopi:"):
        return
    command_id = callback_id.split(":", 1)[1].strip().lower()
    if not command_id:
        return
    args_text = message_text.strip()

    thread_id = _session_thread_id(channel_id, thread_ts)
    response_thread_id = _response_thread_id(cfg, thread_ts=thread_ts)
    command_context = await _resolve_command_context(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if command_context is None:
        return

    reply_ref = None
    reply_text = None
    if isinstance(message_ts, str):
        reply_ref = MessageRef(
            channel_id=channel_id,
            message_id=message_ts,
            thread_id=response_thread_id,
        )
        reply_text = message_text

    handled = await _dispatch_with_command_context(
        cfg,
        command_id=command_id,
        args_text=args_text,
        full_text=f"/{command_id} {args_text}".strip(),
        channel_id=channel_id,
        message_id=message_ts if isinstance(message_ts, str) else "0",
        thread_id=response_thread_id,
        reply_ref=reply_ref,
        reply_text=reply_text,
        running_tasks=running_tasks,
        command_context=command_context,
    )
    if not handled:
        await _respond_ephemeral(
            cfg,
            response_url=response_url if isinstance(response_url, str) else None,
            channel_id=channel_id,
            text=f"unknown command `{command_id}`.",
        )


def _request_cancel(
    running_tasks: RunningTasks,
    channel_id: str,
    message_ts: str,
) -> bool:
    for ref, task in list(running_tasks.items()):
        if str(ref.channel_id) == channel_id and str(ref.message_id) == message_ts:
            task.cancel_requested.set()
            return True
    return False


def _slash_usage() -> str:
    return (
        "usage:\n"
        "/takopi <command> [args]\n\n"
        "or register a dedicated slash command like /takopi-preview and pass args\n"
        "as the text after the command.\n\n"
        "built-ins:\n"
        "/takopi status\n"
        "/takopi engine <engine|clear>\n"
        "/takopi model <engine> <model|clear>\n"
        "/takopi reasoning <engine> <level|clear>\n"
        "/takopi session clear\n"
        "/takopi file <put|get> <path>\n"
    )


def _format_status(state: dict[str, object] | None) -> str:
    if not state:
        return "no thread state found."
    lines = []
    context = state.get("context")
    if isinstance(context, dict):
        project = context.get("project")
        branch = context.get("branch")
        if project:
            if branch:
                lines.append(f"context: `{project}` `@{branch}`")
            else:
                lines.append(f"context: `{project}`")
    default_engine = state.get("default_engine")
    if isinstance(default_engine, str):
        lines.append(f"default engine: `{default_engine}`")
    model_overrides = state.get("model_overrides")
    if isinstance(model_overrides, dict) and model_overrides:
        models = ", ".join(
            f"{engine}={value}"
            for engine, value in sorted(model_overrides.items())
        )
        lines.append(f"model overrides: `{models}`")
    reasoning_overrides = state.get("reasoning_overrides")
    if isinstance(reasoning_overrides, dict) and reasoning_overrides:
        levels = ", ".join(
            f"{engine}={value}"
            for engine, value in sorted(reasoning_overrides.items())
        )
        lines.append(f"reasoning overrides: `{levels}`")
    resumes = state.get("resumes")
    if isinstance(resumes, dict) and resumes:
        lines.append("resume tokens stored: yes")
    if not lines:
        return "thread state is empty."
    return "\n".join(lines)


def _format_worktree_ref(worktree: WorktreeSnapshot) -> str:
    return f"`/{worktree.project}` `@{worktree.branch}`"


def _format_stale_worktree_text(
    *,
    worktree: WorktreeSnapshot,
    hours: float,
    owner_user_id: str | None,
    prefix: str | None = None,
) -> str:
    mention = f"<@{owner_user_id}> " if owner_user_id else ""
    label = _format_worktree_ref(worktree)
    hours_label = _format_hours_label(hours)
    intro = prefix or "Worktree"
    return f"{mention}{intro} {label} has been idle for {hours_label}. Archive it?"


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
) -> tuple[int, str, str]:
    def _exec() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
        )

    completed = await anyio.to_thread.run_sync(_exec)
    return completed.returncode, completed.stdout, completed.stderr


def _safely_resolve_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    try:
        return Path(path)
    except (TypeError, ValueError):
        return None


async def _send_stale_worktree_reminder(
    cfg: SlackBridgeConfig,
    snapshot: ThreadSnapshot,
    *,
    now: float,
) -> None:
    if snapshot.worktree is None:
        return
    text = _format_stale_worktree_text(
        worktree=snapshot.worktree,
        hours=cfg.state.stale_worktree_hours,
        owner_user_id=snapshot.owner_user_id,
    )
    blocks = _build_archive_blocks(
        text,
        thread_id=snapshot.thread_id,
        action_blocks=cfg.state.action_blocks,
    )
    message = RenderedMessage(text=text)
    message.extra["blocks"] = blocks
    sent = await cfg.exec_cfg.transport.send(
        channel_id=snapshot.channel_id,
        message=message,
        options=SendOptions(thread_id=snapshot.thread_id),
    )
    if sent is None or cfg.thread_store is None:
        return
    await cfg.thread_store.set_reminder_sent(
        channel_id=snapshot.channel_id,
        thread_id=snapshot.thread_id,
        now=now,
    )


async def _run_stale_worktree_reminders(cfg: SlackBridgeConfig) -> None:
    if cfg.thread_store is None:
        return
    while True:
        interval_s = max(
            30.0,
            float(cfg.state.stale_worktree_check_interval_s),
        )
        if not cfg.state.stale_worktree_reminder:
            await anyio.sleep(interval_s)
            continue
        stale_s = max(0.0, float(cfg.state.stale_worktree_hours) * 3600.0)
        now = time.time()
        try:
            snapshots = await cfg.thread_store.list_thread_snapshots()
        except Exception as exc:
            logger.exception(
                "slack.stale_worktree_scan_failed",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            await anyio.sleep(interval_s)
            continue

        for snapshot in snapshots:
            if snapshot.worktree is None or snapshot.last_activity_at is None:
                continue
            if now < snapshot.last_activity_at + stale_s:
                continue
            reminder = snapshot.reminder
            if (
                reminder is not None
                and reminder.sent_at is not None
                and reminder.sent_at >= snapshot.last_activity_at
            ):
                continue
            try:
                await _send_stale_worktree_reminder(cfg, snapshot, now=now)
            except Exception as exc:
                logger.exception(
                    "slack.stale_worktree_send_failed",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
        await anyio.sleep(interval_s)


async def _resolve_bot_identity(
    cfg: SlackBridgeConfig,
) -> tuple[str | None, str | None]:
    try:
        auth = await cfg.client.auth_test()
    except SlackApiError as exc:
        logger.warning("slack.auth_test_failed", error=str(exc))
        return None, None
    return auth.user_id, auth.user_name


async def _run_socket_loop(cfg: SlackBridgeConfig) -> None:
    running_tasks: RunningTasks = {}
    backoff_s = 1.0

    async with anyio.create_task_group() as tg:
        if cfg.thread_store is not None:
            tg.start_soon(_run_stale_worktree_reminders, cfg)
        while True:
            app_token = cfg.state.app_token
            if not app_token:
                raise ConfigError("Missing transports.slack.app_token.")

            bot_user_id, bot_name = await _resolve_bot_identity(cfg)
            try:
                socket_url = await open_socket_url(app_token)
            except SlackApiError as exc:
                logger.warning("slack.socket.open_failed", error=str(exc))
                await anyio.sleep(backoff_s)
                continue

            reconnect_requested = False
            try:
                async with websockets.connect(
                    socket_url,
                    ping_interval=10,
                    ping_timeout=10,
                ) as ws:
                    async def _request_reconnect() -> None:
                        await ws.close()

                    cfg.state.reconnect_socket = _request_reconnect
                    while True:
                        if cfg.state.needs_reconnect:
                            reconnect_requested = True
                            cfg.state.needs_reconnect = False
                            logger.info("slack.socket.reconnect_requested")
                            break
                        raw = await ws.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "ignore")
                        try:
                            envelope = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("slack.socket.bad_payload")
                            continue

                        envelope_id = envelope.get("envelope_id")
                        if isinstance(envelope_id, str) and envelope_id:
                            await ws.send(
                                json.dumps({"envelope_id": envelope_id})
                            )

                        msg_type = envelope.get("type")
                        if msg_type == "disconnect":
                            logger.info("slack.socket.disconnect")
                            break
                        if msg_type == "slash_commands":
                            payload = _coerce_socket_payload(
                                envelope.get("payload")
                            )
                            if payload is not None:
                                tg.start_soon(
                                    _handle_slash_command,
                                    cfg,
                                    payload,
                                    running_tasks,
                                )
                            continue
                        if msg_type == "interactive":
                            payload = _coerce_socket_payload(
                                envelope.get("payload")
                            )
                            if payload is not None:
                                tg.start_soon(
                                    _handle_interactive,
                                    cfg,
                                    payload,
                                    running_tasks,
                                )
                            continue
                        if msg_type != "events_api":
                            continue

                        payload = envelope.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        event = payload.get("event")
                        if not isinstance(event, dict):
                            continue

                        event_type = event.get("type")
                        if event_type not in {"message", "app_mention"}:
                            continue
                        channel = event.get("channel")
                        if not _is_allowed_channel(cfg, channel):
                            continue

                        msg = SlackMessage.from_api(event)
                        if _should_skip_message(msg, bot_user_id):
                            continue
                        cleaned = _strip_bot_mention(
                            msg.text or "",
                            bot_user_id=bot_user_id,
                            bot_name=bot_name,
                        )
                        has_files = bool(msg.files)
                        if not _is_allowed_user(cfg, msg.user):
                            if (
                                event_type == "app_mention"
                                and msg.thread_ts is None
                                and (cleaned.strip() or has_files)
                            ):
                                await _request_approval_for_message(
                                    cfg,
                                    message=msg,
                                    text=cleaned,
                                )
                            continue
                        if not _should_process_socket_message(event, msg):
                            continue
                        if not cleaned.strip() and not has_files:
                            continue
                        tg.start_soon(
                            _safe_handle_slack_message,
                            cfg,
                            msg,
                            cleaned,
                            running_tasks,
                        )
                    cfg.state.reconnect_socket = None
            except WebSocketException as exc:
                if not reconnect_requested and not cfg.state.needs_reconnect:
                    logger.warning("slack.socket_failed", error=str(exc))
            except OSError as exc:
                if not reconnect_requested and not cfg.state.needs_reconnect:
                    logger.warning("slack.socket_failed", error=str(exc))
            finally:
                cfg.state.reconnect_socket = None

            if reconnect_requested or cfg.state.needs_reconnect:
                cfg.state.needs_reconnect = False
                continue

            await anyio.sleep(backoff_s)


async def _watch_slack_config(
    cfg: SlackBridgeConfig,
    config_path: Path,
    default_engine_override: str | None,
    transport_id: str,
) -> None:
    async def _on_reload(reload: ConfigReload) -> None:
        try:
            transport_config = reload.settings.transport_config(
                transport_id,
                config_path=reload.config_path,
            )
            settings = SlackTransportSettings.from_config(
                transport_config,
                config_path=reload.config_path,
            )
        except ConfigError as exc:
            logger.warning("slack.reload.failed", error=str(exc))
            return

        await reload_slack_settings(cfg.state, settings, cfg.runtime)

    await core_watch_config(
        config_path=config_path,
        runtime=cfg.runtime,
        default_engine_override=default_engine_override,
        on_reload=_on_reload,
    )


async def run_main_loop(
    cfg: SlackBridgeConfig,
    *,
    watch_config: bool | None = None,
    default_engine_override: str | None = None,
    transport_id: str | None = None,
) -> None:
    await _send_startup(cfg)
    config_path = cfg.runtime.config_path
    async with anyio.create_task_group() as tg:
        tg.start_soon(_run_socket_loop, cfg)
        if watch_config and config_path is not None:
            tg.start_soon(
                _watch_slack_config,
                cfg,
                config_path,
                default_engine_override,
                transport_id or "slack",
            )
