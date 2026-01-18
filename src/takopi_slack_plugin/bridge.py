from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
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
from takopi.directives import parse_directives
from takopi.runners.run_options import EngineRunOptions

from .client import SlackApiError, SlackClient, SlackMessage, open_socket_url
from .commands import dispatch_command, split_command_args
from .engine import run_engine, send_plain
from .outbox import DELETE_PRIORITY, EDIT_PRIORITY, SEND_PRIORITY, OutboxOp, SlackOutbox
from .overrides import REASONING_LEVELS, is_valid_reasoning_level, supports_reasoning
from .thread_sessions import SlackThreadSessionStore

logger = get_logger(__name__)

MAX_SLACK_TEXT = 3900
MAX_BLOCK_TEXT = 2800
CANCEL_ACTION_ID = "takopi-slack:cancel"


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
            if len(chunks) > 1:
                message.extra["followups"] = [
                    RenderedMessage(text=chunk) for chunk in chunks[1:]
                ]
            return message
        rendered = RenderedMessage(text=_trim_text(text, self._max_chars))
        rendered.extra["clear_blocks"] = True
        return rendered


@dataclass(frozen=True, slots=True)
class SlackBridgeConfig:
    client: SlackClient
    runtime: TransportRuntime
    channel_id: str
    app_token: str
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    thread_store: SlackThreadSessionStore | None = None


@dataclass(frozen=True, slots=True)
class CommandContext:
    default_context: RunContext | None
    default_engine_override: str | None
    engine_overrides_resolver: Callable[[str], Awaitable[EngineRunOptions | None]]
    on_thread_known: Callable[[Any, anyio.Event], Awaitable[None]] | None


class SlackTransport:
    def __init__(self, client: SlackClient) -> None:
        self._client = client
        self._outbox = SlackOutbox()
        self._send_counter = 0

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
        self, message: RenderedMessage, *, allow_clear: bool
    ) -> list[dict[str, Any]] | None:
        extra = message.extra
        blocks = extra.get("blocks")
        if isinstance(blocks, list):
            return blocks
        if extra.get("show_cancel"):
            return _build_cancel_blocks(message.text)
        if allow_clear and extra.get("clear_blocks"):
            return []
        return None

    async def close(self) -> None:
        await self._outbox.close()
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
        blocks = self._prepare_blocks(message, allow_clear=False)
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
        blocks = self._prepare_blocks(message, allow_clear=True)
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
        key = self._next_send_key(channel_id)
        op = OutboxOp(
            execute=lambda: self._client.post_message(
                channel_id=channel_id,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
            ),
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


def _mention_regex(bot_user_id: str) -> re.Pattern[str]:
    escaped = re.escape(bot_user_id)
    return re.compile(rf"<@{escaped}(\|[^>]+)?>")


def _strip_bot_mention(text: str, *, bot_user_id: str | None) -> str:
    cleaned = text
    if bot_user_id is not None:
        pattern = _mention_regex(bot_user_id)
        cleaned = pattern.sub("", text)
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
    if message.subtype is not None:
        return True
    if message.bot_id is not None:
        return True
    if message.user is None:
        return True
    if bot_user_id is not None and message.user == bot_user_id:
        return True
    if not message.text or not message.text.strip():
        return True
    return False


async def _send_startup(cfg: SlackBridgeConfig) -> None:
    if not cfg.startup_msg.strip():
        return
    message = RenderedMessage(text=cfg.startup_msg)
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
    channel_id = cfg.channel_id
    is_thread_reply = message.thread_ts is not None
    thread_id = message.thread_ts or message.ts
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
            thread_id=thread_id,
            text=f"error:\n{exc}",
            notify=False,
        )
        return

    context: RunContext | None = None
    engine_override = directives.engine
    prompt = directives.prompt
    if directives.project is not None:
        context = RunContext(project=directives.project, branch=directives.branch)
        if thread_store is not None and thread_id is not None:
            await thread_store.set_context(
                channel_id=channel_id,
                thread_id=thread_id,
                context=context,
            )
            if engine_override is None:
                engine_override = await thread_store.get_default_engine(
                    channel_id=channel_id,
                    thread_id=thread_id,
                )
    elif is_thread_reply and thread_store is not None and thread_id is not None:
        context = await thread_store.get_context(
            channel_id=channel_id,
            thread_id=thread_id,
        )
        if context is not None:
            if directives.branch is not None and context.project is not None:
                context = RunContext(project=context.project, branch=directives.branch)
                await thread_store.set_context(
                    channel_id=channel_id,
                    thread_id=thread_id,
                    context=context,
                )
            if engine_override is None:
                engine_override = await thread_store.get_default_engine(
                    channel_id=channel_id,
                    thread_id=thread_id,
                )

    if directives.project is None and directives.branch is not None and context is None:
        prompt = f"@{directives.branch} {prompt}".strip()

    if not prompt.strip():
        return

    # Router access avoids re-parsing directives in runtime.resolve_message.
    resume_token = cfg.runtime._router.resolve_resume(prompt, None)
    engine_for_session = cfg.runtime.resolve_engine(
        engine_override=engine_override,
        context=context,
    )
    if thread_store is not None and thread_id is not None:
        if resume_token is not None:
            await thread_store.set_resume(
                channel_id=channel_id,
                thread_id=thread_id,
                token=resume_token,
            )
        else:
            resume_token = await thread_store.get_resume(
                channel_id=channel_id,
                thread_id=thread_id,
                engine=engine_for_session,
            )
    run_options = await _resolve_run_options(
        thread_store,
        channel_id=channel_id,
        thread_id=thread_id,
        engine_id=engine_for_session,
    )
    on_thread_known = _make_resume_saver(
        thread_store,
        channel_id=channel_id,
        thread_id=thread_id,
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
        engine_override=engine_override,
        thread_id=thread_id,
        on_thread_known=on_thread_known,
        run_options=run_options,
    )


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


async def _respond_ephemeral(
    cfg: SlackBridgeConfig,
    *,
    response_url: str | None,
    channel_id: str,
    text: str,
) -> None:
    if response_url:
        await cfg.client.post_response(
            response_url=response_url,
            text=text,
            response_type="ephemeral",
        )
        return
    await cfg.client.post_message(channel_id=channel_id, text=text)


def _extract_command_text(tokens: tuple[str, ...], raw_text: str) -> tuple[str, str]:
    head = tokens[0]
    command_id = head.lstrip("/").lower()
    args_text = raw_text[len(head) :].strip()
    return command_id, args_text


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


async def _handle_slash_command(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or channel_id != cfg.channel_id:
        return
    text = payload.get("text") or ""
    response_url = payload.get("response_url")
    thread_ts = _parse_thread_ts(payload.get("thread_ts") or payload.get("message_ts"))
    thread_id = _session_thread_id(channel_id, thread_ts)

    tokens = split_command_args(text)
    if not tokens:
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=_slash_usage(),
        )
        return

    command_id, args_text = _extract_command_text(tokens, text)
    if command_id in {"help", "usage"}:
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=_slash_usage(),
        )
        return

    thread_store = cfg.thread_store
    if thread_store is None:
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text="Slack thread state store is not configured.",
        )
        return

    if command_id == "status":
        state = await thread_store.get_state(
            channel_id=channel_id,
            thread_id=thread_id,
        )
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=_format_status(state),
        )
        return

    if command_id == "engine":
        if len(tokens) < 2:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text="usage: /takopi engine <engine|clear>",
            )
            return
        engine_value = tokens[1].strip()
        if engine_value.lower() == "clear":
            await thread_store.set_default_engine(
                channel_id=channel_id,
                thread_id=thread_id,
                engine=None,
            )
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text="default engine cleared for this thread.",
            )
            return
        engine_id = engine_value.lower()
        if engine_id not in cfg.runtime.engine_ids:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=f"unknown engine: `{engine_value}`",
            )
            return
        await thread_store.set_default_engine(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine_id,
        )
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=f"default engine set to `{engine_id}` for this thread.",
        )
        return

    if command_id == "model":
        if len(tokens) < 3:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text="usage: /takopi model <engine> <model|clear>",
            )
            return
        engine_id = tokens[1].strip().lower()
        model = tokens[2].strip()
        if engine_id not in cfg.runtime.engine_ids:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=f"unknown engine: `{engine_id}`",
            )
            return
        value = None if model.lower() == "clear" else model
        await thread_store.set_model_override(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine_id,
            model=value,
        )
        status = "cleared" if value is None else f"set to `{value}`"
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=f"model override {status} for `{engine_id}`.",
        )
        return

    if command_id == "reasoning":
        if len(tokens) < 3:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text="usage: /takopi reasoning <engine> <level|clear>",
            )
            return
        engine_id = tokens[1].strip().lower()
        level = tokens[2].strip().lower()
        if engine_id not in cfg.runtime.engine_ids:
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=f"unknown engine: `{engine_id}`",
            )
            return
        if level == "clear":
            await thread_store.set_reasoning_override(
                channel_id=channel_id,
                thread_id=thread_id,
                engine=engine_id,
                level=None,
            )
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=f"reasoning override cleared for `{engine_id}`.",
            )
            return
        if not is_valid_reasoning_level(level):
            valid = ", ".join(sorted(REASONING_LEVELS))
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=f"invalid reasoning level. valid: {valid}",
            )
            return
        if not supports_reasoning(engine_id):
            await _respond_ephemeral(
                cfg,
                response_url=response_url,
                channel_id=channel_id,
                text=f"engine `{engine_id}` does not support reasoning overrides.",
            )
            return
        await thread_store.set_reasoning_override(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine_id,
            level=level,
        )
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=f"reasoning override set to `{level}` for `{engine_id}`.",
        )
        return

    if command_id == "session" and len(tokens) >= 2 and tokens[1].lower() == "clear":
        await thread_store.clear_resumes(
            channel_id=channel_id,
            thread_id=thread_id,
        )
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text="resume tokens cleared for this thread.",
        )
        return

    if response_url:
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text="running...",
        )

    command_context = await _resolve_command_context(
        cfg,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if command_context is None:
        return

    handled = await dispatch_command(
        cfg,
        command_id=command_id,
        args_text=args_text,
        full_text=f"/{command_id} {args_text}".strip(),
        channel_id=channel_id,
        message_id="0",
        thread_id=thread_ts,
        reply_ref=None,
        reply_text=None,
        running_tasks=running_tasks,
        on_thread_known=command_context.on_thread_known,
        default_engine_override=command_context.default_engine_override,
        default_context=command_context.default_context,
        engine_overrides_resolver=command_context.engine_overrides_resolver,
    )
    if not handled:
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text=f"unknown command `{command_id}`.",
        )


async def _handle_interactive(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    payload_type = payload.get("type")
    if payload_type == "block_actions":
        await _handle_cancel_action(cfg, payload, running_tasks)
        return
    if payload_type in {"message_action", "shortcut"}:
        await _handle_shortcut(cfg, payload, running_tasks)


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
    await cfg.client.update_message(
        channel_id=channel_id,
        ts=message_ts,
        text=message_text or "cancel requested",
        blocks=[],
    )


async def _handle_shortcut(
    cfg: SlackBridgeConfig,
    payload: dict[str, Any],
    running_tasks: RunningTasks,
) -> None:
    channel = payload.get("channel") or {}
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(channel_id, str) or channel_id != cfg.channel_id:
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

    if response_url:
        await _respond_ephemeral(
            cfg,
            response_url=response_url,
            channel_id=channel_id,
            text="running...",
        )

    thread_id = _session_thread_id(channel_id, thread_ts)
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
            thread_id=thread_ts,
        )
        reply_text = message_text

    handled = await dispatch_command(
        cfg,
        command_id=command_id,
        args_text=args_text,
        full_text=f"/{command_id} {args_text}".strip(),
        channel_id=channel_id,
        message_id=message_ts if isinstance(message_ts, str) else "0",
        thread_id=thread_ts,
        reply_ref=reply_ref,
        reply_text=reply_text,
        running_tasks=running_tasks,
        on_thread_known=command_context.on_thread_known,
        default_engine_override=command_context.default_engine_override,
        default_context=command_context.default_context,
        engine_overrides_resolver=command_context.engine_overrides_resolver,
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
        "built-ins:\n"
        "/takopi status\n"
        "/takopi engine <engine|clear>\n"
        "/takopi model <engine> <model|clear>\n"
        "/takopi reasoning <engine> <level|clear>\n"
        "/takopi session clear\n"
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


async def _run_socket_loop(
    cfg: SlackBridgeConfig,
    *,
    bot_user_id: str | None,
) -> None:
    if not cfg.app_token:
        raise ConfigError(
            "Missing transports.slack.app_token."
        )

    running_tasks: RunningTasks = {}
    backoff_s = 1.0

    async with anyio.create_task_group() as tg:
        while True:
            try:
                socket_url = await open_socket_url(cfg.app_token)
            except SlackApiError as exc:
                logger.warning("slack.socket.open_failed", error=str(exc))
                await anyio.sleep(backoff_s)
                continue

            try:
                async with websockets.connect(
                    socket_url,
                    ping_interval=10,
                    ping_timeout=10,
                ) as ws:
                    while True:
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
                        if channel != cfg.channel_id:
                            continue

                        msg = SlackMessage.from_api(event)
                        if _should_skip_message(msg, bot_user_id):
                            continue
                        cleaned = _strip_bot_mention(
                            msg.text or "",
                            bot_user_id=bot_user_id,
                        )
                        if not cleaned.strip():
                            continue
                        tg.start_soon(
                            _safe_handle_slack_message,
                            cfg,
                            msg,
                            cleaned,
                            running_tasks,
                        )
            except WebSocketException as exc:
                logger.warning("slack.socket_failed", error=str(exc))
            except OSError as exc:
                logger.warning("slack.socket_failed", error=str(exc))

            await anyio.sleep(backoff_s)


async def run_main_loop(
    cfg: SlackBridgeConfig,
    *,
    watch_config: bool | None = None,
    default_engine_override: str | None = None,
    transport_id: str | None = None,
    transport_config: object | None = None,
) -> None:
    _ = watch_config, default_engine_override, transport_id, transport_config
    await _send_startup(cfg)
    bot_user_id: str | None = None
    try:
        auth = await cfg.client.auth_test()
        bot_user_id = auth.user_id
    except SlackApiError as exc:
        logger.warning("slack.auth_test_failed", error=str(exc))

    await _run_socket_loop(cfg, bot_user_id=bot_user_id)
