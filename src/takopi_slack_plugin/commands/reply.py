from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from takopi.transport import RenderedMessage, SendOptions

from ..engine import send_plain

if TYPE_CHECKING:
    from ..bridge import SlackBridgeConfig


def make_reply(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    message_ts: str | None,
    thread_ts: str | None,
) -> Callable[..., Awaitable[None]]:
    state = getattr(cfg, "state", cfg)
    reply_mode = getattr(state, "reply_mode", "thread")
    response_thread_ts = None if reply_mode == "channel" else thread_ts
    if reply_mode != "channel" and response_thread_ts is None:
        response_thread_ts = message_ts

    async def _reply(*, text: str) -> None:
        if message_ts:
            await send_plain(
                cfg.exec_cfg,
                channel_id=channel_id,
                user_msg_id=message_ts,
                thread_id=response_thread_ts,
                text=text,
                notify=True,
            )
            return
        await cfg.exec_cfg.transport.send(
            channel_id=channel_id,
            message=RenderedMessage(text=text),
            options=SendOptions(
                reply_to=None,
                notify=True,
                thread_id=response_thread_ts,
            ),
        )

    return _reply
