from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from takopi.api import get_logger

logger = get_logger(__name__)


class SlackApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SlackAuth:
    user_id: str
    user_name: str | None = None
    team_id: str | None = None
    bot_id: str | None = None


@dataclass(frozen=True, slots=True)
class SlackMessage:
    ts: str
    text: str | None
    user: str | None
    bot_id: str | None
    subtype: str | None
    thread_ts: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "SlackMessage":
        return cls(
            ts=str(payload.get("ts") or ""),
            text=payload.get("text"),
            user=payload.get("user"),
            bot_id=payload.get("bot_id"),
            subtype=payload.get("subtype"),
            thread_ts=payload.get("thread_ts"),
        )


class SlackClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://slack.com/api",
        timeout_s: float = 30.0,
    ) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _request_with_client(
            self._client,
            method,
            endpoint,
            params=params,
            json=json,
        )

    async def auth_test(self) -> SlackAuth:
        payload = await self._request("POST", "/auth.test")
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise SlackApiError("Missing user_id in auth.test response")
        user_name = payload.get("user")
        if not isinstance(user_name, str) or not user_name.strip():
            user_name = None
        return SlackAuth(
            user_id=user_id,
            user_name=user_name,
            team_id=payload.get("team_id"),
            bot_id=payload.get("bot_id"),
        )

    async def post_message(
        self,
        *,
        channel_id: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        reply_broadcast: bool | None = None,
    ) -> SlackMessage:
        data: dict[str, Any] = {
            "channel": channel_id,
            "text": text,
            "mrkdwn": True,
        }
        if blocks is not None:
            data["blocks"] = blocks
        if thread_ts is not None:
            data["thread_ts"] = thread_ts
        if reply_broadcast is not None:
            data["reply_broadcast"] = reply_broadcast
        payload = await self._request("POST", "/chat.postMessage", json=data)
        message = payload.get("message")
        if not isinstance(message, dict):
            raise SlackApiError("Slack postMessage missing message payload")
        return SlackMessage.from_api(message)

    async def update_message(
        self,
        *,
        channel_id: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackMessage:
        data: dict[str, Any] = {
            "channel": channel_id,
            "ts": ts,
            "text": text,
            "mrkdwn": True,
        }
        if blocks is not None:
            data["blocks"] = blocks
        payload = await self._request("POST", "/chat.update", json=data)
        message = payload.get("message")
        if not isinstance(message, dict):
            raise SlackApiError("Slack update missing message payload")
        return SlackMessage.from_api(message)

    async def delete_message(self, *, channel_id: str, ts: str) -> bool:
        data = {"channel": channel_id, "ts": ts}
        await self._request("POST", "/chat.delete", json=data)
        return True

    async def post_response(
        self,
        *,
        response_url: str,
        text: str,
        response_type: str = "ephemeral",
        replace_original: bool | None = None,
        delete_original: bool | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "text": text,
            "response_type": response_type,
        }
        if replace_original is not None:
            payload["replace_original"] = replace_original
        if delete_original is not None:
            payload["delete_original"] = delete_original
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(response_url, json=payload)
            except httpx.HTTPError as exc:
                logger.warning("slack.response_failed", error=str(exc))
                return
        if response.status_code >= 400:
            logger.warning(
                "slack.response_failed",
                status_code=response.status_code,
                body=response.text,
            )

async def _request_with_client(
    client: httpx.AsyncClient,
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    while True:
        try:
            response = await client.request(
                method, endpoint, params=params, json=json
            )
        except httpx.HTTPError as exc:
            logger.warning("slack.network_error", error=str(exc))
            raise SlackApiError("Slack request failed") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = int(retry_after) if retry_after is not None else 1
            except ValueError:
                delay = 1
            logger.info("slack.rate_limited", retry_after=delay)
            await anyio.sleep(delay)
            continue

        if response.status_code >= 400:
            raise SlackApiError(
                f"Slack HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackApiError("Slack response was not JSON") from exc

        if payload.get("ok") is not True:
            error = payload.get("error")
            raise SlackApiError(
                f"Slack API error: {error}",
                error=error,
                status_code=response.status_code,
            )

        return payload


async def open_socket_url(
    app_token: str,
    *,
    base_url: str = "https://slack.com/api",
    timeout_s: float = 30.0,
) -> str:
    token = app_token.strip()
    if not token:
        raise SlackApiError("Missing Slack app token")
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout_s,
    ) as client:
        payload = await _request_with_client(
            client,
            "POST",
            "/apps.connections.open",
        )
    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        raise SlackApiError("Slack socket url missing")
    return url.strip()
