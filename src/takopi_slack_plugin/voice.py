from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from openai import AsyncOpenAI, OpenAIError

from takopi.api import get_logger

from .client import SlackClient
from .commands.file_transfer import SlackFile

logger = get_logger(__name__)

__all__ = ["find_voice_file", "transcribe_voice"]

VOICE_TRANSCRIPTION_DISABLED_HINT = (
    "voice transcription is disabled. enable it in config:\n"
    "```toml\n"
    "[transports.slack]\n"
    "voice_transcription = true\n"
    "```"
)

_AUDIO_FILETYPES = {
    "aac",
    "amr",
    "caf",
    "flac",
    "m4a",
    "mp3",
    "ogg",
    "opus",
    "wav",
    "webm",
}


class VoiceTranscriber(Protocol):
    async def transcribe(self, *, model: str, audio_bytes: bytes) -> str: ...


def _infer_audio_ext(file: SlackFile) -> str:
    mime = (file.mimetype or "").strip().lower()
    # Prefer MIME-based mapping for ambiguous containers like mp4/m4a.
    if mime in {"audio/mp4", "audio/m4a", "audio/x-m4a"}:
        return "m4a"
    if mime in {"audio/mpeg", "audio/mp3"}:
        return "mp3"
    if mime in {"audio/ogg", "audio/opus"}:
        return "ogg"
    if mime in {"audio/wav", "audio/x-wav"}:
        return "wav"
    if mime == "audio/webm":
        return "webm"

    ext = (file.filetype or "").strip().lower().lstrip(".")
    if ext in _AUDIO_FILETYPES:
        return ext
    return "ogg"


def _is_voice_file(file: SlackFile) -> bool:
    mime = (file.mimetype or "").strip().lower()
    if mime.startswith("audio/"):
        return True
    ext = (file.filetype or "").strip().lower().lstrip(".")
    return ext in _AUDIO_FILETYPES


def find_voice_file(files: Sequence[SlackFile]) -> SlackFile | None:
    for file in files:
        if _is_voice_file(file):
            return file
    return None


class OpenAIVoiceTranscriber:
    def __init__(
        self,
        *,
        filename: str = "voice.ogg",
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._filename = filename
        self._base_url = base_url
        self._api_key = api_key

    async def transcribe(self, *, model: str, audio_bytes: bytes) -> str:
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = self._filename
        async with AsyncOpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=120,
        ) as client:
            response = await client.audio.transcriptions.create(
                model=model,
                file=audio_file,
            )
        return response.text


async def transcribe_voice(
    *,
    client: SlackClient,
    file: SlackFile,
    enabled: bool,
    model: str,
    max_bytes: int | None = None,
    reply: Callable[..., Awaitable[None]],
    transcriber: VoiceTranscriber | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str | None:
    if not enabled:
        await reply(text=VOICE_TRANSCRIPTION_DISABLED_HINT)
        return None
    if max_bytes is not None and file.size is not None and file.size > max_bytes:
        await reply(text="voice message is too large to transcribe.")
        return None
    url = file.url_private_download or file.url_private
    if url is None:
        await reply(text="failed to fetch voice file.")
        return None
    audio_bytes = await client.download_file(url=url)
    if audio_bytes is None:
        await reply(text="failed to download voice file.")
        return None
    if max_bytes is not None and len(audio_bytes) > max_bytes:
        await reply(text="voice message is too large to transcribe.")
        return None

    if transcriber is None:
        ext = _infer_audio_ext(file)
        transcriber = OpenAIVoiceTranscriber(
            filename=f"voice.{ext}",
            base_url=base_url,
            api_key=api_key,
        )
    try:
        return await transcriber.transcribe(model=model, audio_bytes=audio_bytes)
    except OpenAIError as exc:
        logger.error(
            "openai.transcribe.error",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        await reply(text=str(exc).strip() or "voice transcription failed")
        return None
    except (RuntimeError, OSError, ValueError) as exc:
        logger.error(
            "voice.transcribe.error",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        await reply(text=str(exc).strip() or "voice transcription failed")
        return None

