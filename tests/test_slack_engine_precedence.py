from __future__ import annotations

from dataclasses import dataclass, field
import importlib.metadata as importlib_metadata
from pathlib import Path
import types

import pytest

_original_version = importlib_metadata.version


def _version(name: str) -> str:
    if name == "takopi":
        return "0.0.0"
    return _original_version(name)


importlib_metadata.version = _version

from takopi.config import ProjectConfig, ProjectsConfig
from takopi.context import RunContext
from takopi.runner_bridge import ExecBridgeConfig
from takopi_slack_plugin import bridge
from takopi_slack_plugin.client import SlackMessage
from tests.slack_fakes import FakeTransport


class _FakeRouter:
    def resolve_resume(self, prompt: str, reply_text: str | None) -> None:
        _ = prompt, reply_text
        return None


@dataclass(slots=True)
class _FakeRuntime:
    _projects: ProjectsConfig
    default_engine: str = "claude"
    engine_ids: tuple[str, ...] = ("claude", "codex")
    allowlist: list[str] | None = None
    _router: _FakeRouter = field(default_factory=_FakeRouter)

    def resolve_engine(
        self,
        *,
        engine_override: str | None,
        context: RunContext | None,
    ) -> str:
        if engine_override is not None:
            return engine_override
        if context is not None and context.project is not None:
            project = self._projects.projects.get(context.project)
            if project is not None and project.default_engine is not None:
                return project.default_engine
        return self.default_engine


class _FakeThreadStore:
    def __init__(self) -> None:
        self.contexts: list[RunContext] = []

    async def set_context(
        self,
        *,
        channel_id: str,
        thread_id: str,
        context: RunContext,
    ) -> None:
        _ = channel_id, thread_id
        self.contexts.append(context)

    async def get_default_engine(
        self,
        *,
        channel_id: str,
        thread_id: str,
    ) -> None:
        _ = channel_id, thread_id
        return None

    async def record_activity(
        self,
        *,
        channel_id: str,
        thread_id: str,
        user_id: str | None,
        worktree,
        clear_worktree: bool,
        now: float,
    ) -> None:
        _ = channel_id, thread_id, user_id, worktree, clear_worktree, now

    async def get_resume(
        self,
        *,
        channel_id: str,
        thread_id: str,
        engine: str,
    ) -> None:
        _ = channel_id, thread_id, engine
        return None

    async def get_model_override(
        self,
        *,
        channel_id: str,
        thread_id: str | None,
        engine: str,
    ) -> None:
        _ = channel_id, thread_id, engine
        return None

    async def get_reasoning_override(
        self,
        *,
        channel_id: str,
        thread_id: str | None,
        engine: str,
    ) -> None:
        _ = channel_id, thread_id, engine
        return None


@pytest.mark.anyio
async def test_project_directive_runs_project_default_engine(monkeypatch) -> None:
    projects = ProjectsConfig(
        projects={
            "zkp2p-node": ProjectConfig(
                alias="zkp2p-node",
                path=Path("/tmp/zkp2p-node"),
                worktrees_dir=Path(".worktrees"),
                default_engine="codex",
            )
        }
    )
    runtime = _FakeRuntime(_projects=projects, default_engine="claude")
    thread_store = _FakeThreadStore()
    cfg = types.SimpleNamespace(
        channel_id="C1",
        state=types.SimpleNamespace(reply_mode="thread"),
        runtime=runtime,
        thread_store=thread_store,
        exec_cfg=ExecBridgeConfig(
            transport=FakeTransport(),
            presenter=object(),
            final_notify=False,
        ),
    )
    message = SlackMessage(
        ts="1710000000.000001",
        text="<@UBOT> /zkp2p-node do a deep code review",
        user="U1",
        bot_id=None,
        subtype=None,
        thread_ts=None,
        channel_id="C1",
    )
    captured: dict[str, object] = {}

    async def _fake_run_engine(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(bridge, "run_engine", _fake_run_engine)

    await bridge._handle_slack_message(
        cfg,
        message,
        "/zkp2p-node do a deep code review",
        running_tasks={},
    )

    assert captured["engine_override"] == "codex"
    assert captured["text"] == "do a deep code review"
    assert captured["context"] == RunContext(project="zkp2p-node", branch=None)
    assert thread_store.contexts == [RunContext(project="zkp2p-node", branch=None)]
