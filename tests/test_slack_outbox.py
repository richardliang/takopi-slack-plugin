from __future__ import annotations

import pytest

from takopi_slack_plugin.outbox import (
    EDIT_PRIORITY,
    SEND_PRIORITY,
    OutboxOp,
    SlackOutbox,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


@pytest.mark.anyio
async def test_outbox_priority_order() -> None:
    clock = _Clock()
    calls: list[str] = []

    async def exec_label(label: str) -> str:
        calls.append(label)
        return label

    class _ManualOutbox(SlackOutbox):
        async def ensure_worker(self) -> None:
            return None

        async def drain(self) -> None:
            while True:
                async with self._cond:
                    picked = self._pick_locked()
                    if picked is None:
                        return
                    key, op = picked
                    self._pending.pop(key, None)
                result = await self._execute_op(op)
                op.set_result(result)

    outbox = _ManualOutbox(
        interval_for_channel=lambda _: 0.0,
        clock=clock,
        sleep=clock.sleep,
    )

    op1 = OutboxOp(
        execute=lambda: exec_label("edit"),
        priority=EDIT_PRIORITY,
        queued_at=2.0,
        channel_id="C1",
    )
    op2 = OutboxOp(
        execute=lambda: exec_label("send-1"),
        priority=SEND_PRIORITY,
        queued_at=2.0,
        channel_id="C1",
    )
    op3 = OutboxOp(
        execute=lambda: exec_label("send-0"),
        priority=SEND_PRIORITY,
        queued_at=1.0,
        channel_id="C1",
    )

    await outbox.enqueue(key="op1", op=op1, wait=False)
    await outbox.enqueue(key="op2", op=op2, wait=False)
    await outbox.enqueue(key="op3", op=op3, wait=False)

    await outbox.drain()
    await op1.done.wait()
    await op2.done.wait()
    await op3.done.wait()

    assert calls == ["send-0", "send-1", "edit"]
    await outbox.close()


@pytest.mark.anyio
async def test_outbox_error_handler() -> None:
    clock = _Clock()
    errors: list[str] = []

    async def boom() -> None:
        raise RuntimeError("nope")

    def on_error(op: OutboxOp, exc: Exception) -> None:
        _ = op
        errors.append(str(exc))

    outbox = SlackOutbox(
        interval_for_channel=lambda _: 0.0,
        clock=clock,
        sleep=clock.sleep,
        on_error=on_error,
    )

    op = OutboxOp(
        execute=boom,
        priority=SEND_PRIORITY,
        queued_at=1.0,
        channel_id="C1",
    )

    result = await outbox.enqueue(key="op", op=op, wait=True)
    assert result is None
    assert errors == ["nope"]
    await outbox.close()


@pytest.mark.anyio
async def test_outbox_replaces_pending_without_worker() -> None:
    class _ManualOutbox(SlackOutbox):
        async def ensure_worker(self) -> None:
            return None

    outbox = _ManualOutbox(interval_for_channel=lambda _: 0.0)

    op1 = OutboxOp(
        execute=lambda: None,
        priority=SEND_PRIORITY,
        queued_at=1.0,
        channel_id="C1",
    )
    op2 = OutboxOp(
        execute=lambda: None,
        priority=SEND_PRIORITY,
        queued_at=2.0,
        channel_id="C1",
    )

    await outbox.enqueue(key="dup", op=op1, wait=False)
    await outbox.enqueue(key="dup", op=op2, wait=False)

    assert op1.done.is_set()
    assert op1.result is None
    assert op2.queued_at == 1.0
