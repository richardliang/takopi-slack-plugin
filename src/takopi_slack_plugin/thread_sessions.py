from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from takopi.api import ResumeToken, RunContext, get_logger

logger = get_logger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "slack_thread_sessions_state.json"


@dataclass(slots=True)
class ThreadSession:
    resumes: dict[str, str] = field(default_factory=dict)
    context: RunContext | None = None


def resolve_sessions_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)


def _thread_key(channel_id: str, thread_id: str) -> str:
    return f"{channel_id}:{thread_id}"


def _encode_context(context: RunContext | None) -> dict[str, str] | None:
    if context is None:
        return None
    payload: dict[str, str] = {}
    if context.project:
        payload["project"] = context.project
    if context.branch:
        payload["branch"] = context.branch
    return payload or None


def _decode_context(payload: object) -> RunContext | None:
    if not isinstance(payload, dict):
        return None
    project = payload.get("project")
    branch = payload.get("branch")
    project_value = project if isinstance(project, str) and project.strip() else None
    branch_value = branch if isinstance(branch, str) and branch.strip() else None
    if project_value is None and branch_value is None:
        return None
    return RunContext(project=project_value, branch=branch_value)


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    )
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(f"{data}\n", encoding="utf-8")
    os.replace(tmp_path, path)


class SlackThreadSessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = anyio.Lock()
        self._loaded = False
        self._mtime_ns: int | None = None
        self._threads: dict[str, ThreadSession] = {}

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _load_locked(self) -> None:
        self._loaded = True
        self._mtime_ns = self._stat_mtime_ns()
        if self._mtime_ns is None:
            self._threads = {}
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "slack.thread_sessions.load_failed",
                path=str(self._path),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            self._threads = {}
            return
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            logger.warning(
                "slack.thread_sessions.version_mismatch",
                path=str(self._path),
                version=payload.get("version") if isinstance(payload, dict) else None,
                expected=STATE_VERSION,
            )
            self._threads = {}
            return
        threads = payload.get("threads")
        if not isinstance(threads, dict):
            self._threads = {}
            return
        parsed: dict[str, ThreadSession] = {}
        for key, entry in threads.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            resumes = entry.get("resumes")
            if not isinstance(resumes, dict):
                resumes = {}
            normalized: dict[str, str] = {}
            for engine_id, resume in resumes.items():
                if isinstance(engine_id, str) and isinstance(resume, str):
                    normalized[engine_id] = resume
            context = _decode_context(entry.get("context"))
            parsed[key] = ThreadSession(resumes=normalized, context=context)
        self._threads = parsed

    def _reload_locked_if_needed(self) -> None:
        current = self._stat_mtime_ns()
        if self._loaded and current == self._mtime_ns:
            return
        self._load_locked()

    def _save_locked(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "threads": {
                key: {
                    "resumes": session.resumes,
                    "context": _encode_context(session.context),
                }
                for key, session in self._threads.items()
            },
        }
        _atomic_write_json(self._path, payload)
        self._mtime_ns = self._stat_mtime_ns()

    async def get_context(
        self, *, channel_id: str, thread_id: str
    ) -> RunContext | None:
        key = _thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._threads.get(key)
            return session.context if session else None

    async def set_context(
        self, *, channel_id: str, thread_id: str, context: RunContext | None
    ) -> None:
        key = _thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._threads.get(key)
            if session is None:
                session = ThreadSession()
                self._threads[key] = session
            session.context = context
            self._save_locked()

    async def get_resume(
        self, *, channel_id: str, thread_id: str, engine: str
    ) -> ResumeToken | None:
        key = _thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._threads.get(key)
            if session is None:
                return None
            value = session.resumes.get(engine)
            if not value:
                return None
            return ResumeToken(engine=engine, value=value)

    async def set_resume(
        self, *, channel_id: str, thread_id: str, token: ResumeToken
    ) -> None:
        key = _thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            session = self._threads.get(key)
            if session is None:
                session = ThreadSession()
                self._threads[key] = session
            session.resumes[token.engine] = token.value
            self._save_locked()

    async def clear_thread(self, *, channel_id: str, thread_id: str) -> None:
        key = _thread_key(channel_id, thread_id)
        async with self._lock:
            self._reload_locked_if_needed()
            if key not in self._threads:
                return
            self._threads.pop(key, None)
            self._save_locked()
