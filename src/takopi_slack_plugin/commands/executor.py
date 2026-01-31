from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import anyio

from takopi.commands import CommandExecutor, RunMode, RunRequest, RunResult
from takopi.context import RunContext
from takopi.model import EngineId, ResumeToken
from takopi.runner_bridge import ExecBridgeConfig, RunningTasks
from takopi.runners.run_options import EngineRunOptions
from takopi.transport import MessageRef, RenderedMessage, SendOptions
from takopi.transport_runtime import TransportRuntime

from ..engine import run_engine


class _CaptureTransport:
    def __init__(self) -> None:
        self._next_id = 1
        self.last_message: RenderedMessage | None = None

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        thread_id = options.thread_id if options is not None else None
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.last_message = message
        return MessageRef(
            channel_id=ref.channel_id,
            message_id=ref.message_id,
            thread_id=thread_id,
        )

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef:
        _ = wait
        self.last_message = message
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        _ = ref
        return True

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class SlackCommandExecutor(CommandExecutor):
    exec_cfg: ExecBridgeConfig
    runtime: TransportRuntime
    running_tasks: RunningTasks
    on_thread_known: Callable[[ResumeToken, anyio.Event], Awaitable[None]] | None
    engine_overrides_resolver: Callable[
        [EngineId], Awaitable[EngineRunOptions | None]
    ] | None
    env_overrides: dict[str, str] | None
    channel_id: str
    user_msg_id: str
    thread_id: str | None
    show_resume_line: bool
    default_engine_override: EngineId | None
    default_context: RunContext | None

    def _apply_default_context(self, request: RunRequest) -> RunRequest:
        if request.context is not None or self.default_context is None:
            return request
        return RunRequest(
            prompt=request.prompt,
            engine=request.engine,
            context=self.default_context,
        )

    def _apply_default_engine(self, request: RunRequest) -> RunRequest:
        if request.engine is not None or self.default_engine_override is None:
            return request
        return RunRequest(
            prompt=request.prompt,
            engine=self.default_engine_override,
            context=request.context,
        )

    async def send(
        self,
        message: RenderedMessage | str,
        *,
        reply_to: MessageRef | None = None,
        notify: bool = True,
    ) -> MessageRef | None:
        rendered = (
            message
            if isinstance(message, RenderedMessage)
            else RenderedMessage(text=message)
        )
        reply_ref = (
            MessageRef(
                channel_id=self.channel_id,
                message_id=self.user_msg_id,
                thread_id=self.thread_id,
            )
            if reply_to is None
            else reply_to
        )
        return await self.exec_cfg.transport.send(
            channel_id=self.channel_id,
            message=rendered,
            options=SendOptions(
                reply_to=reply_ref,
                notify=notify,
                thread_id=self.thread_id,
            ),
        )

    async def run_one(
        self, request: RunRequest, *, mode: RunMode = "emit"
    ) -> RunResult:
        request = self._apply_default_context(request)
        request = self._apply_default_engine(request)
        engine = self.runtime.resolve_engine(
            engine_override=request.engine,
            context=request.context,
        )
        run_options = None
        if self.engine_overrides_resolver is not None:
            run_options = await self.engine_overrides_resolver(engine)

        if mode == "capture":
            capture = _CaptureTransport()
            exec_cfg = ExecBridgeConfig(
                transport=capture,
                presenter=self.exec_cfg.presenter,
                final_notify=False,
            )
            await run_engine(
                exec_cfg=exec_cfg,
                runtime=self.runtime,
                running_tasks={},
                channel_id=self.channel_id,
                user_msg_id=self.user_msg_id,
                text=request.prompt,
                resume_token=None,
                context=request.context,
                engine_override=engine,
                thread_id=self.thread_id,
                on_thread_known=self.on_thread_known,
                run_options=run_options,
                env_overrides=self.env_overrides,
            )
            return RunResult(engine=engine, message=capture.last_message)

        await run_engine(
            exec_cfg=self.exec_cfg,
            runtime=self.runtime,
            running_tasks=self.running_tasks,
            channel_id=self.channel_id,
            user_msg_id=self.user_msg_id,
            text=request.prompt,
            resume_token=None,
            context=request.context,
            engine_override=engine,
            thread_id=self.thread_id,
            on_thread_known=self.on_thread_known,
            run_options=run_options,
            env_overrides=self.env_overrides,
        )
        return RunResult(engine=engine, message=None)

    async def run_many(
        self,
        requests: Sequence[RunRequest],
        *,
        mode: RunMode = "emit",
        parallel: bool = False,
    ) -> list[RunResult]:
        if not parallel:
            return [await self.run_one(request, mode=mode) for request in requests]
        results: list[RunResult | None] = [None] * len(requests)

        async with anyio.create_task_group() as tg:

            async def run_idx(idx: int, request: RunRequest) -> None:
                results[idx] = await self.run_one(request, mode=mode)

            for idx, request in enumerate(requests):
                tg.start_soon(run_idx, idx, request)

        return [result for result in results if result is not None]
