from __future__ import annotations

import pytest

from takopi.transport import MessageRef, RenderedMessage, SendOptions
from takopi_slack_plugin.bridge import SlackTransport
from takopi_slack_plugin.client import SlackApiError, SlackMessage


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
    def __init__(self) -> None:
        self.post_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.fail_thread_error: str | None = None

    async def post_message(
        self,
        *,
        channel_id: str,
        text: str,
        blocks=None,
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
        if thread_ts and self.fail_thread_error:
            error = self.fail_thread_error
            self.fail_thread_error = None
            raise SlackApiError("boom", error=error)
        return SlackMessage(
            ts="1",
            text=text,
            user=None,
            bot_id=None,
            subtype=None,
            thread_ts=thread_ts,
        )

    async def update_message(
        self, *, channel_id: str, ts: str, text: str, blocks=None
    ) -> SlackMessage:
        self.update_calls.append({"channel_id": channel_id, "ts": ts, "text": text})
        return SlackMessage(
            ts=ts,
            text=text,
            user=None,
            bot_id=None,
            subtype=None,
            thread_ts=None,
        )

    async def delete_message(self, *, channel_id: str, ts: str) -> bool:
        self.delete_calls.append({"channel_id": channel_id, "ts": ts})
        return True

    async def close(self) -> None:
        return None


@pytest.mark.anyio
async def test_send_thread_fallback() -> None:
    client = _FakeSlackClient()
    client.fail_thread_error = "invalid_thread_ts"
    transport = SlackTransport(client)
    transport._outbox = _ImmediateOutbox()

    message = RenderedMessage(text="hello")
    options = SendOptions(thread_id="1.1", reply_to=MessageRef(channel_id="C1", message_id="1"))

    ref = await transport.send(channel_id="C1", message=message, options=options)

    assert ref is not None
    assert len(client.post_calls) == 2
    assert client.post_calls[0]["thread_ts"] == "1.1"
    assert client.post_calls[1]["thread_ts"] is None


@pytest.mark.anyio
async def test_delete_uses_outbox() -> None:
    client = _FakeSlackClient()
    transport = SlackTransport(client)
    transport._outbox = _ImmediateOutbox()

    ok = await transport.delete(ref=MessageRef(channel_id="C1", message_id="1"))
    assert ok is True
    assert client.delete_calls == [{"channel_id": "C1", "ts": "1"}]
