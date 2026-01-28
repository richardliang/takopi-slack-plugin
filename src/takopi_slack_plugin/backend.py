from __future__ import annotations

import os
import shutil
from pathlib import Path

import anyio

from takopi.api import (
    EngineBackend,
    SetupIssue,
    SetupResult,
    TransportBackend,
    TransportRuntime,
    ExecBridgeConfig,
    ConfigError,
    HOME_CONFIG_PATH,
    install_issue,
    load_settings,
)

from .bridge import SlackBridgeConfig, SlackPresenter, SlackTransport, run_main_loop
from .client import SlackClient
from .config import SlackTransportSettings
from .onboarding import interactive_setup
from .thread_sessions import SlackThreadSessionStore, resolve_sessions_path

_CREATE_CONFIG_TITLE = "create a config"
_CONFIGURE_SLACK_TITLE = "configure slack"


def _config_issue(path: Path, *, title: str) -> SetupIssue:
    display = _display_path(path)
    return SetupIssue(title, (f"   {display}",))


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _expect_transport_settings(
    transport_config: object, *, config_path: Path
) -> SlackTransportSettings:
    return SlackTransportSettings.from_config(
        transport_config, config_path=config_path
    )


def check_setup(
    backend: EngineBackend,
    *,
    transport_override: str | None = None,
) -> SetupResult:
    issues: list[SetupIssue] = []
    config_path = HOME_CONFIG_PATH
    cmd = backend.cli_cmd or backend.id
    backend_issues: list[SetupIssue] = []
    if shutil.which(cmd) is None:
        backend_issues.append(install_issue(cmd, backend.install_cmd))

    try:
        settings, config_path = load_settings()
        if transport_override:
            settings = settings.model_copy(update={"transport": transport_override})
        if settings.transport != "slack":
            issues.append(_config_issue(config_path, title=_CONFIGURE_SLACK_TITLE))
        else:
            try:
                transport_config = settings.transport_config(
                    "slack", config_path=config_path
                )
                _expect_transport_settings(
                    transport_config, config_path=config_path
                )
            except ConfigError:
                issues.append(_config_issue(config_path, title=_CONFIGURE_SLACK_TITLE))
    except ConfigError:
        issues.extend(backend_issues)
        title = (
            _CONFIGURE_SLACK_TITLE
            if config_path.exists() and config_path.is_file()
            else _CREATE_CONFIG_TITLE
        )
        issues.append(_config_issue(config_path, title=title))
        return SetupResult(issues=issues, config_path=config_path)

    issues.extend(backend_issues)
    return SetupResult(issues=issues, config_path=config_path)


class SlackBackend(TransportBackend):
    id = "slack"
    description = "Slack bot"

    def check_setup(
        self,
        engine_backend: EngineBackend,
        *,
        transport_override: str | None = None,
    ) -> SetupResult:
        return check_setup(engine_backend, transport_override=transport_override)

    async def interactive_setup(self, *, force: bool) -> bool:
        return await interactive_setup(force=force)

    def lock_token(self, *, transport_config: object, _config_path: Path) -> str | None:
        settings = _expect_transport_settings(
            transport_config, config_path=_config_path
        )
        return settings.bot_token

    def build_and_run(
        self,
        *,
        transport_config: object,
        config_path: Path,
        runtime: TransportRuntime,
        final_notify: bool,
        default_engine_override: str | None,
    ) -> None:
        settings = _expect_transport_settings(
            transport_config, config_path=config_path
        )
        startup_msg = _build_startup_message(runtime, startup_pwd=os.getcwd())
        client = SlackClient(settings.bot_token)
        transport = SlackTransport(client)
        presenter = SlackPresenter(message_overflow=settings.message_overflow)
        exec_cfg = ExecBridgeConfig(
            transport=transport,
            presenter=presenter,
            final_notify=final_notify,
        )
        thread_store = SlackThreadSessionStore(
            resolve_sessions_path(config_path)
        )
        cfg = SlackBridgeConfig(
            client=client,
            runtime=runtime,
            channel_id=settings.channel_id,
            app_token=settings.app_token,
            startup_msg=startup_msg,
            exec_cfg=exec_cfg,
            files=settings.files,
            thread_store=thread_store,
            stale_worktree_reminder=settings.stale_worktree_reminder,
            stale_worktree_hours=settings.stale_worktree_hours,
            stale_worktree_check_interval_s=settings.stale_worktree_check_interval_s,
        )

        async def run_loop() -> None:
            await run_main_loop(
                cfg,
                watch_config=runtime.watch_config,
                default_engine_override=default_engine_override,
                transport_id=self.id,
                transport_config=settings,
            )

        anyio.run(run_loop)


def _build_startup_message(runtime: TransportRuntime, *, startup_pwd: str) -> str:
    available_engines = list(runtime.available_engine_ids())
    missing_engines = list(runtime.missing_engine_ids())
    misconfigured_engines = list(runtime.engine_ids_with_status("bad_config"))
    failed_engines = list(runtime.engine_ids_with_status("load_error"))

    engine_list = ", ".join(available_engines) if available_engines else "none"

    notes: list[str] = []
    if missing_engines:
        notes.append(f"not installed: {', '.join(missing_engines)}")
    if misconfigured_engines:
        notes.append(f"misconfigured: {', '.join(misconfigured_engines)}")
    if failed_engines:
        notes.append(f"failed to load: {', '.join(failed_engines)}")
    if notes:
        engine_list = f"{engine_list} ({'; '.join(notes)})"

    project_aliases = sorted(set(runtime.project_aliases()), key=str.lower)
    project_list = ", ".join(project_aliases) if project_aliases else "none"

    return (
        "takopi is ready\n\n"
        f"default: `{runtime.default_engine}`\n"
        f"agents: `{engine_list}`\n"
        f"projects: `{project_list}`\n"
        f"working in: `{startup_pwd}`"
    )


slack_backend = SlackBackend()
BACKEND = slack_backend
