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
    ConfigError,
    HOME_CONFIG_PATH,
    install_issue,
    load_settings,
)

from .bridge import (
    SlackBridgeConfig,
    build_startup_message,
    create_reloadable_slack_state,
    run_main_loop,
)
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
        startup_pwd = os.getcwd()
        startup_msg = build_startup_message(runtime, startup_pwd=startup_pwd)
        thread_store = SlackThreadSessionStore(
            resolve_sessions_path(config_path)
        )
        state, exec_cfg = create_reloadable_slack_state(
            settings,
            startup_pwd=startup_pwd,
            startup_msg=startup_msg,
            final_notify=final_notify,
        )
        cfg = SlackBridgeConfig(
            runtime=runtime,
            channel_id=settings.channel_id,
            exec_cfg=exec_cfg,
            state=state,
            thread_store=thread_store,
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


slack_backend = SlackBackend()
BACKEND = slack_backend
