from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from typing import Any, Awaitable, Callable

import anyio

from takopi.api import (
    ConfigError,
    ExecBridgeConfig,
    IncomingMessage as RunnerIncomingMessage,
    MessageRef,
    RenderedMessage,
    RunContext,
    RunnerUnavailableError,
    RunningTasks,
    SendOptions,
    TransportRuntime,
    bind_run_context,
    clear_context,
    handle_message,
    reset_run_base_dir,
    set_run_base_dir,
)
from takopi.runners.run_options import EngineRunOptions, apply_run_options


# Serialize runs to avoid leaking per-user env overrides across concurrent runs.
_RUN_ENV_LOCK = anyio.Lock()


@asynccontextmanager
async def _apply_env_overrides(
    env_overrides: dict[str, str] | None,
) -> AsyncIterator[None]:
    async with _RUN_ENV_LOCK:
        if not env_overrides:
            yield
            return
        previous = {key: os.environ.get(key) for key in env_overrides}
        os.environ.update(env_overrides)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


async def send_plain(
    exec_cfg: ExecBridgeConfig,
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
    return await exec_cfg.transport.send(
        channel_id=channel_id,
        message=RenderedMessage(text=text),
        options=SendOptions(reply_to=reply_ref, notify=notify, thread_id=thread_id),
    )


async def run_engine(
    *,
    exec_cfg: ExecBridgeConfig,
    runtime: TransportRuntime,
    running_tasks: RunningTasks,
    channel_id: str,
    user_msg_id: str,
    text: str,
    resume_token,
    context: RunContext | None,
    engine_override,
    thread_id: str | None,
    on_thread_known: Callable[[Any, anyio.Event], Awaitable[None]] | None = None,
    run_options: EngineRunOptions | None = None,
    env_overrides: dict[str, str] | None = None,
) -> None:
    try:
        try:
            entry = runtime.resolve_runner(
                resume_token=resume_token,
                engine_override=engine_override,
            )
        except RunnerUnavailableError as exc:
            await send_plain(
                exec_cfg,
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
            await send_plain(
                exec_cfg,
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
            await send_plain(
                exec_cfg,
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
            async with _apply_env_overrides(env_overrides):
                with apply_run_options(run_options):
                    await handle_message(
                        exec_cfg,
                        runner=runner,
                        incoming=incoming,
                        resume_token=resume_token,
                        context=context,
                        context_line=context_line,
                        strip_resume_line=runtime.is_resume_line,
                        running_tasks=running_tasks,
                        on_thread_known=on_thread_known,
                    )
        finally:
            reset_run_base_dir(run_base_token)
    finally:
        clear_context()
