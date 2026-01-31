from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable

import anyio
from takopi.commands import CommandContext, get_command
from takopi.config import ConfigError
from takopi.logging import get_logger
from takopi.model import EngineId, ResumeToken
from takopi.runner_bridge import RunningTasks
from takopi.runners.run_options import EngineRunOptions
from takopi.transport import MessageRef

from .executor import SlackCommandExecutor

logger = get_logger(__name__)


def split_command_args(text: str) -> tuple[str, ...]:
    if not text.strip():
        return ()
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return tuple(text.split())


async def dispatch_command(
    cfg,
    *,
    command_id: str,
    args_text: str,
    full_text: str,
    channel_id: str,
    message_id: str,
    thread_id: str | None,
    reply_ref: MessageRef | None,
    reply_text: str | None,
    running_tasks: RunningTasks,
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None,
    default_engine_override: EngineId | None,
    default_context,
    engine_overrides_resolver: Callable[[EngineId], Awaitable[EngineRunOptions | None]]
    | None,
    env_overrides: dict[str, str] | None,
) -> bool:
    allowlist = cfg.runtime.allowlist

    executor = SlackCommandExecutor(
        exec_cfg=cfg.exec_cfg,
        runtime=cfg.runtime,
        running_tasks=running_tasks,
        on_thread_known=on_thread_known,
        engine_overrides_resolver=engine_overrides_resolver,
        env_overrides=env_overrides,
        channel_id=channel_id,
        user_msg_id=message_id,
        thread_id=thread_id,
        show_resume_line=True,
        default_engine_override=default_engine_override,
        default_context=default_context,
    )

    message_ref = MessageRef(
        channel_id=channel_id,
        message_id=message_id,
        thread_id=thread_id,
    )

    try:
        backend = get_command(command_id, allowlist=allowlist, required=False)
    except ConfigError as exc:
        await executor.send(f"error:\n{exc}", reply_to=message_ref, notify=True)
        return True

    if backend is None:
        return False

    try:
        plugin_config = cfg.runtime.plugin_config(command_id)
    except ConfigError as exc:
        await executor.send(f"error:\n{exc}", reply_to=message_ref, notify=True)
        return True

    ctx = CommandContext(
        command=command_id,
        text=full_text,
        args_text=args_text,
        args=split_command_args(args_text),
        message=message_ref,
        reply_to=reply_ref,
        reply_text=reply_text,
        config_path=cfg.runtime.config_path,
        plugin_config=plugin_config,
        runtime=cfg.runtime,
        executor=executor,
    )

    try:
        result = await backend.handle(ctx)
    except Exception as exc:
        logger.exception(
            "command.failed",
            command=command_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        await executor.send(f"error:\n{exc}", reply_to=message_ref, notify=True)
        return True

    if result is not None:
        reply_to = message_ref if result.reply_to is None else result.reply_to
        await executor.send(result.text, reply_to=reply_to, notify=result.notify)

    return True
