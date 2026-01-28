from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import msgspec

from takopi.api import ResumeToken, RunContext, get_logger
from takopi.telegram.state_store import JsonStateStore

logger = get_logger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "slack_thread_sessions_state.json"


class _ThreadSession(msgspec.Struct, forbid_unknown_fields=False):
    resumes: dict[str, str] = msgspec.field(default_factory=dict)
    context: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None
    reasoning_overrides: dict[str, str] | None = None
    default_engine: str | None = None
    last_activity_at: float | None = None
    owner_user_id: str | None = None
    worktree: _WorktreeRef | None = None
    reminder: _ReminderState | None = None


class _WorktreeRef(msgspec.Struct, forbid_unknown_fields=False):
    project: str
    branch: str


class _ReminderState(msgspec.Struct, forbid_unknown_fields=False):
    sent_at: float | None = None


class _ThreadSessionsState(msgspec.Struct, forbid_unknown_fields=False):
    version: int
    threads: dict[str, _ThreadSession] = msgspec.field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    project: str
    branch: str


@dataclass(frozen=True, slots=True)
class ReminderSnapshot:
    sent_at: float | None


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    channel_id: str
    thread_id: str
    last_activity_at: float | None
    owner_user_id: str | None
    worktree: WorktreeSnapshot | None
    reminder: ReminderSnapshot | None


def resolve_sessions_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)


def _thread_key(channel_id: str, thread_id: str) -> str:
    return f"{channel_id}:{thread_id}"


def _split_thread_key(key: str) -> tuple[str, str] | None:
    if ":" not in key:
        return None
    channel_id, thread_id = key.split(":", 1)
    if not channel_id or not thread_id:
        return None
    return channel_id, thread_id


def _new_state() -> _ThreadSessionsState:
    return _ThreadSessionsState(version=STATE_VERSION, threads={})


class SlackThreadSessionStore(JsonStateStore[_ThreadSessionsState]):
    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            version=STATE_VERSION,
            state_type=_ThreadSessionsState,
            state_factory=_new_state,
            log_prefix="slack.thread_sessions",
            logger=logger,
        )

    @staticmethod
    def _thread_key(channel_id: str, thread_id: str) -> str:
        return _thread_key(channel_id, thread_id)

    def _get_or_create(self, key: str) -> _ThreadSession:
        session = self._state.threads.get(key)
        if session is None:
            session = _ThreadSession()
            self._state.threads[key] = session
        return session

    @staticmethod
    def _snapshot_from_session(
        channel_id: str,
        thread_id: str,
        session: _ThreadSession,
    ) -> ThreadSnapshot:
        worktree = None
        if session.worktree is not None:
            worktree = WorktreeSnapshot(
                project=session.worktree.project,
                branch=session.worktree.branch,
            )
        reminder = None
        if session.reminder is not None:
            reminder = ReminderSnapshot(
                sent_at=session.reminder.sent_at,
            )
        return ThreadSnapshot(
            channel_id=channel_id,
            thread_id=thread_id,
            last_activity_at=session.last_activity_at,
            owner_user_id=session.owner_user_id,
            worktree=worktree,
            reminder=reminder,
        )

    async def get_resume(
        self, *, channel_id: str, thread_id: str, engine: str
    ) -> ResumeToken | None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return None
            value = session.resumes.get(engine)
            if not value:
                return None
            return ResumeToken(engine=engine, value=value)

    async def set_resume(
        self, *, channel_id: str, thread_id: str, token: ResumeToken
    ) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._get_or_create(key)
            session.resumes[token.engine] = token.value
            self._save_locked()

    async def record_activity(
        self,
        *,
        channel_id: str,
        thread_id: str,
        user_id: str | None,
        worktree: WorktreeSnapshot | None,
        clear_worktree: bool,
        now: float,
    ) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._get_or_create(key)
            session.last_activity_at = now
            if user_id and not session.owner_user_id:
                session.owner_user_id = user_id
            if worktree is not None:
                session.worktree = _WorktreeRef(
                    project=worktree.project,
                    branch=worktree.branch,
                )
            elif clear_worktree:
                session.worktree = None
            reminder = session.reminder
            if reminder is None:
                reminder = _ReminderState()
                session.reminder = reminder
            reminder.sent_at = None
            self._save_locked()

    async def set_reminder_sent(
        self,
        *,
        channel_id: str,
        thread_id: str,
        now: float,
    ) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._get_or_create(key)
            reminder = session.reminder
            if reminder is None:
                reminder = _ReminderState()
                session.reminder = reminder
            reminder.sent_at = now
            self._save_locked()

    async def clear_worktree(self, *, channel_id: str, thread_id: str) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return
            session.worktree = None
            session.reminder = None
            self._save_locked()

    async def get_thread_snapshot(
        self, *, channel_id: str, thread_id: str
    ) -> ThreadSnapshot | None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return None
            return self._snapshot_from_session(channel_id, thread_id, session)

    async def list_thread_snapshots(self) -> list[ThreadSnapshot]:
        async with self._lock:
            self._reload_locked_if_needed()
            snapshots: list[ThreadSnapshot] = []
            for key, session in self._state.threads.items():
                parsed = _split_thread_key(key)
                if parsed is None:
                    continue
                channel_id, thread_id = parsed
                snapshots.append(
                    self._snapshot_from_session(channel_id, thread_id, session)
                )
            return snapshots

    async def clear_thread(self, *, channel_id: str, thread_id: str) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            if key not in self._state.threads:
                return
            self._state.threads.pop(key, None)
            self._save_locked()

    async def clear_resumes(self, *, channel_id: str, thread_id: str) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return
            session.resumes = {}
            self._save_locked()

    async def get_context(
        self, *, channel_id: str, thread_id: str
    ) -> RunContext | None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None or session.context is None:
                return None
            project = session.context.get("project")
            if not project:
                return None
            branch = session.context.get("branch")
            return RunContext(project=project, branch=branch)

    async def set_context(
        self,
        *,
        channel_id: str,
        thread_id: str,
        context: RunContext | None,
    ) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._get_or_create(key)
            if context is None:
                session.context = None
            else:
                payload: dict[str, str] = {"project": context.project}
                if context.branch:
                    payload["branch"] = context.branch
                session.context = payload
            self._save_locked()

    async def get_default_engine(
        self, *, channel_id: str, thread_id: str
    ) -> str | None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return None
            return session.default_engine

    async def get_state(
        self, *, channel_id: str, thread_id: str
    ) -> dict[str, object] | None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return None
            return {
                "context": dict(session.context) if session.context else None,
                "default_engine": session.default_engine,
                "model_overrides": dict(session.model_overrides)
                if session.model_overrides
                else None,
                "reasoning_overrides": dict(session.reasoning_overrides)
                if session.reasoning_overrides
                else None,
                "resumes": dict(session.resumes) if session.resumes else None,
            }

    async def set_default_engine(
        self,
        *,
        channel_id: str,
        thread_id: str,
        engine: str | None,
    ) -> None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._get_or_create(key)
            session.default_engine = _normalize_override(engine)
            self._save_locked()

    async def get_model_override(
        self, *, channel_id: str, thread_id: str, engine: str
    ) -> str | None:
        return await self._get_override(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine,
            field="model_overrides",
        )

    async def set_model_override(
        self,
        *,
        channel_id: str,
        thread_id: str,
        engine: str,
        model: str | None,
    ) -> None:
        await self._set_override(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine,
            value=model,
            field="model_overrides",
        )

    async def get_reasoning_override(
        self, *, channel_id: str, thread_id: str, engine: str
    ) -> str | None:
        return await self._get_override(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine,
            field="reasoning_overrides",
        )

    async def set_reasoning_override(
        self,
        *,
        channel_id: str,
        thread_id: str,
        engine: str,
        level: str | None,
    ) -> None:
        await self._set_override(
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine,
            value=level,
            field="reasoning_overrides",
        )

    async def _get_override(
        self,
        *,
        channel_id: str,
        thread_id: str,
        engine: str,
        field: str,
    ) -> str | None:
        key = self._thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._state.threads.get(key)
            if session is None:
                return None
            overrides = getattr(session, field)
            if not isinstance(overrides, dict):
                return None
            value = overrides.get(engine)
            return _normalize_override(value)

    async def _set_override(
        self,
        *,
        channel_id: str,
        thread_id: str,
        engine: str,
        value: str | None,
        field: str,
    ) -> None:
        key = self._thread_key(channel_id, thread_id)
        normalized = _normalize_override(value)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._get_or_create(key)
            overrides = getattr(session, field)
            if overrides is None or not isinstance(overrides, dict):
                overrides = {}
                setattr(session, field, overrides)
            if normalized is None:
                overrides.pop(engine, None)
                if not overrides:
                    setattr(session, field, None)
            else:
                overrides[engine] = normalized
            self._save_locked()


def _normalize_override(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
