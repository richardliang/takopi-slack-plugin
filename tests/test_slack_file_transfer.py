from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from takopi.api import RunContext
from takopi.runner_bridge import ExecBridgeConfig
from takopi_slack_plugin.config import SlackFilesSettings
from takopi_slack_plugin.commands.file_transfer import (
    SlackFile,
    extract_files,
    handle_file_command,
)
from tests.slack_fakes import FakeTransport


class _FakeClient:
    def __init__(self) -> None:
        self.download_calls: list[str] = []
        self.upload_calls: list[dict] = []

    async def download_file(self, *, url: str) -> bytes | None:
        self.download_calls.append(url)
        return b"hello"

    async def upload_file(
        self,
        *,
        channel_id: str,
        filename: str,
        content: bytes,
        thread_ts: str | None = None,
        initial_comment: str | None = None,
    ) -> dict:
        self.upload_calls.append(
            {
                "channel_id": channel_id,
                "filename": filename,
                "content": content,
                "thread_ts": thread_ts,
                "initial_comment": initial_comment,
            }
        )
        return {"id": "F1"}


@dataclass(slots=True)
class _FakeRuntime:
    run_root: Path

    def resolve_message(self, *, text: str, reply_text=None, ambient_context=None, chat_id=None):
        _ = reply_text, ambient_context, chat_id
        return SimpleNamespace(prompt=text, context=RunContext(project="proj"))

    def resolve_run_cwd(self, context: RunContext | None) -> Path | None:
        _ = context
        return self.run_root

    def format_context_line(self, context: RunContext | None) -> str | None:
        _ = context
        return "`ctx: proj`"

    @property
    def config_path(self) -> Path | None:
        return None


@pytest.mark.anyio
async def test_handle_file_put_saves_file(tmp_path) -> None:
    fake_client = _FakeClient()
    transport = FakeTransport()
    cfg = SimpleNamespace(
        client=fake_client,
        runtime=_FakeRuntime(tmp_path),
        files=SlackFilesSettings(enabled=True),
        exec_cfg=ExecBridgeConfig(transport=transport, presenter=object(), final_notify=False),
    )
    file = SlackFile(
        file_id="F1",
        name="note.txt",
        size=5,
        mimetype="text/plain",
        filetype="txt",
        url_private="https://example.com",
        url_private_download=None,
        mode=None,
    )

    await handle_file_command(
        cfg,
        channel_id="C1",
        message_ts="1",
        thread_ts="1",
        user_id="U1",
        args_text="put note.txt",
        files=[file],
        ambient_context=None,
    )

    target = tmp_path / "note.txt"
    assert target.exists()
    assert target.read_bytes() == b"hello"
    assert transport.send_calls


@pytest.mark.anyio
async def test_handle_file_get_uploads_file(tmp_path) -> None:
    fake_client = _FakeClient()
    transport = FakeTransport()
    cfg = SimpleNamespace(
        client=fake_client,
        runtime=_FakeRuntime(tmp_path),
        files=SlackFilesSettings(enabled=True),
        exec_cfg=ExecBridgeConfig(transport=transport, presenter=object(), final_notify=False),
    )
    path = tmp_path / "note.txt"
    path.write_bytes(b"data")

    await handle_file_command(
        cfg,
        channel_id="C1",
        message_ts="1",
        thread_ts="1",
        user_id="U1",
        args_text="get note.txt",
        files=[],
        ambient_context=None,
    )

    assert fake_client.upload_calls


def test_extract_files() -> None:
    payload = [
        {"id": "F1", "url_private": "https://example.com", "filetype": "mp3"},
        {"id": "F2", "url_private": "https://example.com", "mimetype": "text/plain"},
    ]
    files = extract_files(payload)
    assert len(files) == 2
