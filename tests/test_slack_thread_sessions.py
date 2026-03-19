from pathlib import Path

import pytest

from takopi.api import ResumeToken, RunContext
from takopi_slack_plugin.thread_sessions import SlackThreadSessionStore


@pytest.mark.anyio
async def test_thread_sessions_resume_roundtrip(tmp_path) -> None:
    path = tmp_path / "slack_thread_sessions_state.json"
    store = SlackThreadSessionStore(path)

    await store.set_resume(
        channel_id="C1",
        thread_id="T1",
        token=ResumeToken(engine="codex", value="abc"),
    )
    assert await store.get_resume(
        channel_id="C1", thread_id="T1", engine="codex"
    ) == ResumeToken(engine="codex", value="abc")

    store2 = SlackThreadSessionStore(path)
    assert await store2.get_resume(
        channel_id="C1", thread_id="T1", engine="codex"
    ) == ResumeToken(engine="codex", value="abc")


@pytest.mark.anyio
async def test_thread_sessions_context_and_overrides(tmp_path) -> None:
    path = tmp_path / "slack_thread_sessions_state.json"
    store = SlackThreadSessionStore(path)

    context = RunContext(project="proj", branch="feat")
    await store.set_context(channel_id="C1", thread_id="T1", context=context)
    assert await store.get_context(channel_id="C1", thread_id="T1") == context

    await store.set_default_engine(
        channel_id="C1", thread_id="T1", engine="  codex  "
    )
    assert await store.get_default_engine(channel_id="C1", thread_id="T1") == "codex"

    await store.set_model_override(
        channel_id="C1", thread_id="T1", engine="codex", model=" gpt-4o "
    )
    await store.set_reasoning_override(
        channel_id="C1", thread_id="T1", engine="codex", level=" high "
    )
    assert await store.get_model_override(
        channel_id="C1", thread_id="T1", engine="codex"
    ) == "gpt-4o"
    assert await store.get_reasoning_override(
        channel_id="C1", thread_id="T1", engine="codex"
    ) == "high"

    await store.set_model_override(
        channel_id="C1", thread_id="T1", engine="codex", model=None
    )
    await store.set_reasoning_override(
        channel_id="C1", thread_id="T1", engine="codex", level=None
    )
    assert await store.get_model_override(
        channel_id="C1", thread_id="T1", engine="codex"
    ) is None
    assert await store.get_reasoning_override(
        channel_id="C1", thread_id="T1", engine="codex"
    ) is None


@pytest.mark.anyio
async def test_thread_sessions_clear_and_state(tmp_path) -> None:
    path = tmp_path / "slack_thread_sessions_state.json"
    store = SlackThreadSessionStore(path)

    await store.set_resume(
        channel_id="C1",
        thread_id="T1",
        token=ResumeToken(engine="codex", value="one"),
    )
    await store.set_context(
        channel_id="C1", thread_id="T1", context=RunContext(project="proj")
    )

    state = await store.get_state(channel_id="C1", thread_id="T1")
    assert state and state["context"]["project"] == "proj"

    await store.clear_resumes(channel_id="C1", thread_id="T1")
    assert await store.get_resume(
        channel_id="C1", thread_id="T1", engine="codex"
    ) is None

    await store.clear_thread(channel_id="C1", thread_id="T1")
    assert await store.get_context(channel_id="C1", thread_id="T1") is None


@pytest.mark.anyio
async def test_thread_sessions_pending_approval_roundtrip(tmp_path) -> None:
    path = tmp_path / "slack_thread_sessions_state.json"
    store = SlackThreadSessionStore(path)

    created = await store.set_pending_approval(
        channel_id="C1",
        thread_id="T1",
        requester_user_id="U2",
        source_message_ts="111.222",
        response_thread_id="111.222",
        cleaned_text="hello there",
        created_at=123.0,
        files=[{"id": "F1"}],
    )
    assert created.status == "pending"
    assert created.requester_user_id == "U2"
    assert created.files == [{"id": "F1"}]

    updated = await store.set_pending_approval_message(
        channel_id="C1",
        thread_id="T1",
        approval_message_ts="333.444",
    )
    assert updated is not None
    assert updated.approval_message_ts == "333.444"

    resolved = await store.resolve_pending_approval(
        channel_id="C1",
        thread_id="T1",
        status="approved",
        decided_by_user_id="U1",
        decided_at=456.0,
    )
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.decided_by_user_id == "U1"
    assert resolved.decided_at == 456.0

    store2 = SlackThreadSessionStore(path)
    loaded = await store2.get_pending_approval(channel_id="C1", thread_id="T1")
    assert loaded is not None
    assert loaded.status == "approved"
    assert loaded.approval_message_ts == "333.444"
