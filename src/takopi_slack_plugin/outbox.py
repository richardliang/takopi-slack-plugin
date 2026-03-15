from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import anyio

__all__ = [
    "DELETE_PRIORITY",
    "EDIT_PRIORITY",
    "SEND_PRIORITY",
    "OutboxOp",
    "SlackOutbox",
]

SEND_PRIORITY = 0
DELETE_PRIORITY = 1
EDIT_PRIORITY = 2

DEFAULT_CHANNEL_INTERVAL = 0.3


@dataclass(slots=True)
class OutboxOp:
    execute: callable
    priority: int
    queued_at: float
    channel_id: str | None
    label: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None

    def set_result(self, result: Any) -> None:
        if self.done.is_set():
            return
        self.result = result
        self.done.set()


class SlackOutbox:
    def __init__(
        self,
        *,
        interval_for_channel: callable | None = None,
        clock: callable = time.monotonic,
        sleep: callable = anyio.sleep,
        on_error: callable | None = None,
        on_outbox_error: callable | None = None,
    ) -> None:
        self._interval_for_channel = interval_for_channel or (
            lambda _: DEFAULT_CHANNEL_INTERVAL
        )
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error
        self._on_outbox_error = on_outbox_error
        self._pending: dict[object, OutboxOp] = {}
        self._cond = anyio.Condition()
        self._start_lock = anyio.Lock()
        self._closed = False
        self._worker_task: asyncio.Task[None] | None = None
        self._next_at = 0.0

    async def ensure_worker(self) -> None:
        async with self._start_lock:
            if self._worker_task is not None or self._closed:
                return
            self._worker_task = asyncio.create_task(self._run())

    async def enqueue(self, *, key: object, op: OutboxOp, wait: bool = True) -> Any:
        await self.ensure_worker()
        async with self._cond:
            if self._closed:
                op.set_result(None)
                return op.result
            previous = self._pending.get(key)
            if previous is not None:
                op.queued_at = previous.queued_at
                previous.set_result(None)
            self._pending[key] = op
            self._cond.notify()
        if not wait:
            return None
        await op.done.wait()
        return op.result

    async def drop_pending(self, *, key: object) -> None:
        async with self._cond:
            pending = self._pending.pop(key, None)
            if pending is not None:
                pending.set_result(None)
            self._cond.notify()

    async def close(self, *, drain: bool = False) -> None:
        async with self._cond:
            self._closed = True
            if not drain:
                self._fail_pending()
            self._cond.notify_all()
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None

    def _fail_pending(self) -> None:
        for pending in list(self._pending.values()):
            pending.set_result(None)
        self._pending.clear()

    def _pick_locked(self) -> tuple[object, OutboxOp] | None:
        if not self._pending:
            return None
        return min(
            self._pending.items(),
            key=lambda item: (item[1].priority, item[1].queued_at),
        )

    async def _execute_op(self, op: OutboxOp) -> Any:
        try:
            return await op.execute()
        except Exception as exc:  # noqa: BLE001
            if self._on_error is not None:
                self._on_error(op, exc)
            return None

    async def _sleep_until(self, deadline: float) -> None:
        delay = deadline - self._clock()
        if delay > 0:
            await self._sleep(delay)

    async def _run(self) -> None:
        cancel_exc = anyio.get_cancelled_exc_class()
        try:
            while True:
                async with self._cond:
                    while not self._pending and not self._closed:
                        await self._cond.wait()
                    if self._closed and not self._pending:
                        return

                if self._clock() < self._next_at:
                    await self._sleep_until(self._next_at)
                    continue

                async with self._cond:
                    if self._closed and not self._pending:
                        return
                    picked = self._pick_locked()
                    if picked is None:
                        continue
                    key, op = picked
                    self._pending.pop(key, None)

                interval = self._interval_for_channel(op.channel_id)
                if interval:
                    self._next_at = max(self._next_at, self._clock()) + interval
                result = await self._execute_op(op)
                op.set_result(result)
        except cancel_exc:
            return
        except Exception as exc:  # noqa: BLE001
            self._fail_pending()
            if self._on_outbox_error is not None:
                self._on_outbox_error(exc)
            return
