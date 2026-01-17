from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import anyio
import websockets
from websockets.exceptions import WebSocketException

from takopi.api import (
    ConfigError,
    DirectiveError,
    ExecBridgeConfig,
    IncomingMessage as RunnerIncomingMessage,
    MessageRef,
    RenderedMessage,
    RunnerUnavailableError,
    RunningTasks,
    SendOptions,
    Transport,
    TransportRuntime,
    bind_run_context,
    clear_context,
    get_logger,
    handle_message,
    reset_run_base_dir,
    set_run_base_dir,
)

from .client import SlackApiError, SlackClient, SlackMessage, open_socket_url

logger = get_logger(__name__)

MAX_SLACK_TEXT = 3900


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
        return RenderedMessage(text=_trim_text(text, self._max_chars))

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
            if len(chunks) > 1:
                message.extra["followups"] = [
                    RenderedMessage(text=chunk) for chunk in chunks[1:]
                ]
            return message
        return RenderedMessage(text=_trim_text(text, self._max_chars))


@dataclass(frozen=True, slots=True)
class SlackBridgeConfig:
    client: SlackClient
    runtime: TransportRuntime
    channel_id: str
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    poll_interval_s: float = 1.0
    reply_in_thread: bool = False
    require_mention: bool = False
    socket_mode: bool = False
    app_token: str | None = None


class SlackTransport:
    def __init__(self, client: SlackClient) -> None:
        self._client = client

    @staticmethod
    def _extract_followups(message: RenderedMessage) -> list[RenderedMessage]:
        followups = message.extra.get("followups")
        if not isinstance(followups, list):
            return []
        return [item for item in followups if isinstance(item, RenderedMessage)]

    async def close(self) -> None:
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
        sent = await self._client.post_message(
            channel_id=channel,
            text=message.text,
            thread_ts=thread_ts,
        )
        ref = MessageRef(
            channel_id=channel,
            message_id=sent.ts,
            raw=sent,
            thread_id=thread_ts,
        )
        if options is not None and options.replace is not None:
            await self._client.delete_message(
                channel_id=channel,
                ts=str(options.replace.message_id),
            )
        for followup in followups:
            await self._client.post_message(
                channel_id=channel,
                text=followup.text,
                thread_ts=thread_ts,
            )
        return ref

    async def edit(
        self,
        *,
        ref: MessageRef,
        message: RenderedMessage,
        wait: bool = True,
    ) -> MessageRef | None:
        _ = wait
        updated = await self._client.update_message(
            channel_id=str(ref.channel_id),
            ts=str(ref.message_id),
            text=message.text,
        )
        return MessageRef(
            channel_id=ref.channel_id,
            message_id=updated.ts,
            raw=updated,
            thread_id=ref.thread_id,
        )

    async def delete(self, *, ref: MessageRef) -> bool:
        return await self._client.delete_message(
            channel_id=str(ref.channel_id),
            ts=str(ref.message_id),
        )


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


def _parse_ts(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mention_regex(bot_user_id: str) -> re.Pattern[str]:
    escaped = re.escape(bot_user_id)
    return re.compile(rf"<@{escaped}(\|[^>]+)?>")


def _strip_bot_mention(
    text: str, *, bot_user_id: str | None, require_mention: bool
) -> tuple[str, bool]:
    cleaned = text
    if require_mention:
        if bot_user_id is None:
            return text, True
        pattern = _mention_regex(bot_user_id)
        if not pattern.search(text):
            return text, False
        cleaned = pattern.sub("", text)
    return cleaned.strip(), True


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


async def _send_plain(
    transport: Transport,
    *,
    channel_id: str,
    user_msg_id: str,
    thread_id: str | None,
    text: str,
    notify: bool = True,
) -> MessageRef | None:
    reply_ref = MessageRef(
        channel_id=channel_id,
        message_id=user_msg_id,
        thread_id=thread_id,
    )
    return await transport.send(
        channel_id=channel_id,
        message=RenderedMessage(text=text),
        options=SendOptions(reply_to=reply_ref, notify=notify, thread_id=thread_id),
    )


async def _run_engine(
    *,
    exec_cfg: ExecBridgeConfig,
    runtime: TransportRuntime,
    running_tasks: RunningTasks,
    channel_id: str,
    user_msg_id: str,
    text: str,
    resume_token,
    context,
    engine_override,
    thread_id: str | None,
) -> None:
    try:
        try:
            entry = runtime.resolve_runner(
                resume_token=resume_token,
                engine_override=engine_override,
            )
        except RunnerUnavailableError as exc:
            await _send_plain(
                exec_cfg.transport,
                channel_id=channel_id,
                user_msg_id=user_msg_id,
                thread_id=thread_id,
                text=f"error:\n{exc}",
                notify=False,
            )
            return

        runner = entry.runner
        if not entry.available:
            reason = entry.issue or "engine unavailable"
            await _send_plain(
                exec_cfg.transport,
                channel_id=channel_id,
                user_msg_id=user_msg_id,
                thread_id=thread_id,
                text=f"error:\n{reason}",
                notify=False,
            )
            return

        try:
            cwd = runtime.resolve_run_cwd(context)
        except ConfigError as exc:
            await _send_plain(
                exec_cfg.transport,
                channel_id=channel_id,
                user_msg_id=user_msg_id,
                thread_id=thread_id,
                text=f"error:\n{exc}",
                notify=False,
            )
            return

        run_base_token = set_run_base_dir(cwd)
        try:
            run_fields: dict[str, Any] = {
                "channel_id": channel_id,
                "user_msg_id": user_msg_id,
                "engine": runner.engine,
                "resume": resume_token.value if resume_token else None,
            }
            if context is not None:
                run_fields["project"] = context.project
                run_fields["branch"] = context.branch
            if cwd is not None:
                run_fields["cwd"] = str(cwd)
            bind_run_context(**run_fields)
            context_line = runtime.format_context_line(context)
            incoming = RunnerIncomingMessage(
                channel_id=channel_id,
                message_id=user_msg_id,
                text=text,
                thread_id=thread_id,
            )
            await handle_message(
                exec_cfg,
                runner=runner,
                incoming=incoming,
                resume_token=resume_token,
                context=context,
                context_line=context_line,
                strip_resume_line=runtime.is_resume_line,
                running_tasks=running_tasks,
            )
        finally:
            reset_run_base_dir(run_base_token)
    except Exception as exc:
        logger.exception(
            "slack.run_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
    finally:
        clear_context()


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
    thread_id = message.thread_ts if cfg.reply_in_thread and message.thread_ts else None
    if cfg.reply_in_thread and thread_id is None:
        thread_id = message.ts
    try:
        resolved = cfg.runtime.resolve_message(
            text=text,
            reply_text=None,
            ambient_context=None,
            chat_id=None,
        )
    except DirectiveError as exc:
        await _send_plain(
            cfg.exec_cfg.transport,
            channel_id=channel_id,
            user_msg_id=message.ts,
            thread_id=thread_id,
            text=f"error:\n{exc}",
            notify=False,
        )
        return

    prompt = resolved.prompt
    if not prompt.strip():
        return

    await _run_engine(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=running_tasks,
        channel_id=channel_id,
        user_msg_id=message.ts,
        text=prompt,
        resume_token=resolved.resume_token,
        context=resolved.context,
        engine_override=resolved.engine_override,
        thread_id=thread_id,
    )


async def _safe_handle_slack_message(
    cfg: SlackBridgeConfig,
    message: SlackMessage,
    text: str,
    running_tasks: RunningTasks,
) -> None:
    try:
        await _handle_slack_message(
            cfg,
            message=message,
            text=text,
            running_tasks=running_tasks,
        )
    except Exception as exc:
        logger.exception(
            "slack.message_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


def _resolve_mention_requirement(
    cfg: SlackBridgeConfig, bot_user_id: str | None
) -> bool:
    mention_required = cfg.require_mention
    if mention_required and bot_user_id is None:
        logger.warning("slack.mention_disabled", reason="bot_user_id missing")
        mention_required = False
    return mention_required


async def _run_polling_loop(
    cfg: SlackBridgeConfig,
    *,
    bot_user_id: str | None,
) -> None:
    last_seen_value = 0.0
    last_seen_raw: str | None = None
    try:
        seed = await cfg.client.conversations_history(
            channel_id=cfg.channel_id, limit=1
        )
        if seed:
            last_seen_value = _parse_ts(seed[0].ts)
            last_seen_raw = seed[0].ts
    except SlackApiError as exc:
        logger.warning("slack.seed_failed", error=str(exc))

    running_tasks: RunningTasks = {}
    mention_required = _resolve_mention_requirement(cfg, bot_user_id)

    async with anyio.create_task_group() as tg:
        while True:
            try:
                messages = await cfg.client.conversations_history(
                    channel_id=cfg.channel_id,
                    oldest=last_seen_raw,
                )
            except SlackApiError as exc:
                logger.warning("slack.poll_failed", error=str(exc))
                await anyio.sleep(cfg.poll_interval_s)
                continue
            messages.sort(key=lambda msg: _parse_ts(msg.ts))
            for msg in messages:
                ts_value = _parse_ts(msg.ts)
                if ts_value <= last_seen_value:
                    continue
                last_seen_value = ts_value
                last_seen_raw = msg.ts
                if _should_skip_message(msg, bot_user_id):
                    continue
                cleaned, allowed = _strip_bot_mention(
                    msg.text or "",
                    bot_user_id=bot_user_id,
                    require_mention=mention_required,
                )
                if not allowed:
                    continue
                if not cleaned.strip():
                    continue
                tg.start_soon(
                    _safe_handle_slack_message,
                    cfg,
                    msg,
                    cleaned,
                    running_tasks,
                )
            await anyio.sleep(cfg.poll_interval_s)


async def _run_socket_mode_loop(
    cfg: SlackBridgeConfig,
    *,
    bot_user_id: str | None,
) -> None:
    if not cfg.app_token:
        raise ConfigError(
            "Missing transports.slack.app_token for socket_mode."
        )

    running_tasks: RunningTasks = {}
    mention_required = _resolve_mention_requirement(cfg, bot_user_id)
    backoff_s = max(1.0, float(cfg.poll_interval_s))

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
                        cleaned, allowed = _strip_bot_mention(
                            msg.text or "",
                            bot_user_id=bot_user_id,
                            require_mention=mention_required,
                        )
                        if not allowed:
                            continue
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

    if cfg.socket_mode:
        await _run_socket_mode_loop(cfg, bot_user_id=bot_user_id)
    else:
        await _run_polling_loop(cfg, bot_user_id=bot_user_id)
