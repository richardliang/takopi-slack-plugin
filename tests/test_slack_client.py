from __future__ import annotations

from functools import partial

import anyio
import httpx
import pytest

from takopi_slack_plugin.client import (
    SlackApiError,
    SlackClient,
    SlackMessage,
    _request_with_client,
    open_socket_url,
)


@pytest.mark.anyio
async def test_request_with_client_rate_limit(monkeypatch) -> None:
    calls: list[int] = []

    async def _sleep(delay: float) -> None:
        calls.append(int(delay))

    monkeypatch.setattr("takopi_slack_plugin.client.anyio.sleep", _sleep)

    request = httpx.Request("POST", "https://example.com")
    responses = [
        httpx.Response(429, request=request, headers={"Retry-After": "1"}),
        httpx.Response(200, request=request, json={"ok": True, "value": 1}),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        payload = await _request_with_client(client, "POST", "/test")

    assert payload["value"] == 1
    assert calls == [1]


@pytest.mark.anyio
async def test_request_with_client_errors() -> None:
    request = httpx.Request("POST", "https://example.com")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"ok": False, "error": "bad"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        with pytest.raises(SlackApiError) as exc:
            await _request_with_client(client, "POST", "/test")

    assert exc.value.error == "bad"


@pytest.mark.anyio
async def test_request_with_client_bad_json() -> None:
    request = httpx.Request("POST", "https://example.com")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"nope")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        with pytest.raises(SlackApiError):
            await _request_with_client(client, "POST", "/test")


@pytest.mark.anyio
async def test_open_socket_url(monkeypatch) -> None:
    request = httpx.Request("POST", "https://example.com")
    real_async_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"ok": True, "url": "wss://x"})

    transport = httpx.MockTransport(handler)

    class _StubAsyncClient:
        def __init__(self, **kwargs) -> None:
            _ = kwargs
            self._client = real_async_client(
                transport=transport,
                base_url="https://example.com",
            )

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, exc_type, exc, tb) -> None:
            await self._client.aclose()

    monkeypatch.setattr("takopi_slack_plugin.client.httpx.AsyncClient", _StubAsyncClient)

    url = await open_socket_url("xapp-token", base_url="https://example.com")
    assert url == "wss://x"


def test_open_socket_url_missing_token() -> None:
    with pytest.raises(SlackApiError):
        anyio.run(open_socket_url, " ")


@pytest.mark.anyio
async def test_upload_file_uses_files_upload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/files.upload"
        return httpx.Response(200, request=request, json={"ok": True, "file": {"id": "F1"}})

    transport = httpx.MockTransport(handler)
    client = SlackClient("token", base_url="https://example.com")
    client._client = httpx.AsyncClient(transport=transport, base_url="https://example.com")

    result = await client.upload_file(
        channel_id="C1", filename="note.txt", content=b"hi"
    )
    assert result["id"] == "F1"
    await client.close()


def test_client_methods_build_payloads() -> None:
    class _StubClient(SlackClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None]] = []

        async def _request(
            self,
            method: str,
            endpoint: str,
            *,
            params: dict | None = None,
            json: dict | None = None,
            data: dict | None = None,
            files: dict | None = None,
        ) -> dict:
            _ = params, data, files
            self.calls.append((method, endpoint, json))
            if endpoint == "/auth.test":
                return {"user_id": "U1", "user": "bot", "team_id": "T1", "bot_id": "B1"}
            if endpoint == "/chat.postMessage":
                return {"message": {"ts": "1", "text": "hi"}}
            if endpoint == "/chat.update":
                return {"message": {"ts": "1", "text": "edit"}}
            if endpoint == "/chat.delete":
                return {"ok": True}
            return {"ok": True}

    client = _StubClient()

    auth = anyio.run(client.auth_test)
    assert auth.user_id == "U1"

    msg = anyio.run(
        partial(
            client.post_message,
            channel_id="C1",
            text="hello",
            blocks=[{"type": "section"}],
            thread_ts="1.1",
            reply_broadcast=True,
        )
    )
    assert isinstance(msg, SlackMessage)

    anyio.run(
        partial(
            client.update_message,
            channel_id="C1",
            ts="1.1",
            text="edit",
            blocks=None,
        )
    )

    anyio.run(partial(client.delete_message, channel_id="C1", ts="1.1"))

    post_call = next(call for call in client.calls if call[1] == "/chat.postMessage")
    assert post_call[2]["channel"] == "C1"
    assert post_call[2]["text"] == "hello"
    assert post_call[2]["thread_ts"] == "1.1"
    assert post_call[2]["reply_broadcast"] is True


def test_auth_test_missing_user_id() -> None:
    class _StubClient(SlackClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None]] = []

        async def _request(
            self,
            method: str,
            endpoint: str,
            *,
            params: dict | None = None,
            json: dict | None = None,
            data: dict | None = None,
            files: dict | None = None,
        ) -> dict:
            _ = method, endpoint, params, json, data, files
            return {"ok": True}

    client = _StubClient()
    with pytest.raises(SlackApiError):
        anyio.run(client.auth_test)
