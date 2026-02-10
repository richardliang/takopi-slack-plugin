from __future__ import annotations

from types import SimpleNamespace

import pytest

from takopi.runner_bridge import ExecBridgeConfig
from takopi_slack_plugin.bridge import _resolve_prompt_from_media
from takopi_slack_plugin.client import SlackMessage
from takopi_slack_plugin.commands.file_transfer import SlackFile
from takopi_slack_plugin.voice import (
    VOICE_TRANSCRIPTION_DISABLED_HINT,
    find_voice_file,
    transcribe_voice,
)
from tests.slack_fakes import FakeTransport


class _FakeClient:
    def __init__(self, *, audio: bytes | None = b"ok") -> None:
        self._audio = audio
        self.download_calls: list[str] = []

    async def download_file(self, *, url: str) -> bytes | None:
        self.download_calls.append(url)
        return self._audio


class _FakeTranscriber:
    def __init__(self, *, text: str = "hello") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def transcribe(self, *, model: str, audio_bytes: bytes) -> str:
        self.calls.append({"model": model, "audio_bytes": audio_bytes})
        return self.text


def _audio_file(*, size: int | None = 10, url: str | None = "https://example.com") -> SlackFile:
    return SlackFile(
        file_id="F1",
        name="voice.wav",
        size=size,
        mimetype="audio/wav",
        filetype="wav",
        url_private=url,
        url_private_download=None,
        mode=None,
    )


def test_find_voice_file() -> None:
    audio = _audio_file()
    non_audio = SlackFile(
        file_id="F2",
        name="note.txt",
        size=5,
        mimetype="text/plain",
        filetype="txt",
        url_private="https://example.com/2",
        url_private_download=None,
        mode=None,
    )
    assert find_voice_file([non_audio, audio]) == audio
    assert find_voice_file([non_audio]) is None


@pytest.mark.anyio
async def test_transcribe_voice_disabled_replies() -> None:
    client = _FakeClient()
    file = _audio_file()
    replies: list[str] = []

    async def _reply(*, text: str) -> None:
        replies.append(text)

    result = await transcribe_voice(
        client=client,
        file=file,
        enabled=False,
        model="gpt-4o-mini-transcribe",
        max_bytes=1024,
        reply=_reply,
        transcriber=_FakeTranscriber(),
    )
    assert result is None
    assert replies == [VOICE_TRANSCRIPTION_DISABLED_HINT]
    assert client.download_calls == []


@pytest.mark.anyio
async def test_transcribe_voice_too_large_replies() -> None:
    client = _FakeClient()
    file = _audio_file(size=2048)
    replies: list[str] = []

    async def _reply(*, text: str) -> None:
        replies.append(text)

    result = await transcribe_voice(
        client=client,
        file=file,
        enabled=True,
        model="gpt-4o-mini-transcribe",
        max_bytes=1024,
        reply=_reply,
        transcriber=_FakeTranscriber(),
    )
    assert result is None
    assert replies == ["voice message is too large to transcribe."]
    assert client.download_calls == []


@pytest.mark.anyio
async def test_transcribe_voice_success() -> None:
    client = _FakeClient(audio=b"bytes")
    file = _audio_file(size=4)
    replies: list[str] = []
    transcriber = _FakeTranscriber(text="transcribed")

    async def _reply(*, text: str) -> None:
        replies.append(text)

    result = await transcribe_voice(
        client=client,
        file=file,
        enabled=True,
        model="gpt-4o-mini-transcribe",
        max_bytes=1024,
        reply=_reply,
        transcriber=transcriber,
    )
    assert result == "transcribed"
    assert replies == []
    assert client.download_calls == ["https://example.com"]
    assert transcriber.calls == [
        {"model": "gpt-4o-mini-transcribe", "audio_bytes": b"bytes"}
    ]


@pytest.mark.anyio
async def test_bridge_resolve_prompt_transcribes_voice(monkeypatch) -> None:
    transport = FakeTransport()
    cfg = SimpleNamespace(
        channel_id="C1",
        client=_FakeClient(audio=b"bytes"),
        voice_transcription=True,
        voice_max_bytes=1024,
        voice_transcription_model="gpt-4o-mini-transcribe",
        voice_transcription_base_url=None,
        voice_transcription_api_key=None,
        exec_cfg=ExecBridgeConfig(transport=transport, presenter=object(), final_notify=False),
    )

    calls: list[dict] = []

    async def _fake_transcribe_voice(**kwargs):
        calls.append(kwargs)
        return "transcribed"

    def _should_not_call(*args, **kwargs):
        raise AssertionError("handle_file_uploads should not be called for voice files")

    monkeypatch.setattr("takopi_slack_plugin.bridge.transcribe_voice", _fake_transcribe_voice)
    monkeypatch.setattr("takopi_slack_plugin.bridge.handle_file_uploads", _should_not_call)

    msg = SlackMessage(
        ts="1",
        text="",
        user="U1",
        bot_id=None,
        subtype="file_share",
        thread_ts=None,
        files=[
            {
                "id": "F1",
                "mimetype": "audio/wav",
                "filetype": "wav",
                "size": 4,
                "url_private": "https://example.com",
            }
        ],
    )

    resolved = await _resolve_prompt_from_media(
        cfg,
        message=msg,
        prompt="",
        context=None,
        thread_id="1",
    )
    assert resolved == "transcribed"
    assert calls

