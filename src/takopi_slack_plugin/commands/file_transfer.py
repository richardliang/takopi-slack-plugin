from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TYPE_CHECKING

from takopi.api import ConfigError, DirectiveError, RunContext, get_logger
from takopi.telegram.files import (
    ZipTooLargeError,
    default_upload_name,
    deny_reason,
    file_usage,
    format_bytes,
    normalize_relative_path,
    parse_file_command,
    parse_file_prompt,
    resolve_path_within_root,
    write_bytes_atomic,
    zip_directory,
)

from ..client import SlackApiError
from .reply import make_reply

if TYPE_CHECKING:
    from ..bridge import SlackBridgeConfig

logger = get_logger(__name__)

FILE_PUT_USAGE = "usage: `/file put <path>`"
FILE_GET_USAGE = "usage: `/file get <path>`"

@dataclass(frozen=True, slots=True)
class SlackFile:
    file_id: str
    name: str | None
    size: int | None
    mimetype: str | None
    filetype: str | None
    url_private: str | None
    url_private_download: str | None
    mode: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "SlackFile" | None:
        file_id = payload.get("id")
        if not isinstance(file_id, str) or not file_id:
            return None
        url_private = payload.get("url_private")
        url_private_download = payload.get("url_private_download")
        if not isinstance(url_private, str):
            url_private = None
        if not isinstance(url_private_download, str):
            url_private_download = None
        if url_private is None and url_private_download is None:
            return None
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            name = None
        size = payload.get("size")
        if not isinstance(size, int) or size < 0:
            size = None
        mimetype = payload.get("mimetype")
        if not isinstance(mimetype, str) or not mimetype.strip():
            mimetype = None
        filetype = payload.get("filetype")
        if not isinstance(filetype, str) or not filetype.strip():
            filetype = None
        mode = payload.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            mode = None
        return cls(
            file_id=file_id,
            name=name,
            size=size,
            mimetype=mimetype,
            filetype=filetype,
            url_private=url_private,
            url_private_download=url_private_download,
            mode=mode,
        )


@dataclass(frozen=True, slots=True)
class FileSaveResult:
    name: str
    rel_path: Path | None
    size: int | None
    error: str | None


def extract_files(files: object) -> list[SlackFile]:
    if not isinstance(files, list):
        return []
    parsed: list[SlackFile] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        parsed_file = SlackFile.from_api(item)
        if parsed_file is not None:
            parsed.append(parsed_file)
    return parsed


def _format_context(cfg: SlackBridgeConfig, context: RunContext | None) -> str:
    context_line = cfg.runtime.format_context_line(context)
    return context_line or "`ctx: default`"


def _resolve_put_paths(
    path_value: str | None,
    *,
    uploads_dir: str,
    deny_globs: Sequence[str],
    require_dir: bool,
) -> tuple[Path | None, Path | None, str | None]:
    path_value = (path_value or "").strip()
    if not path_value:
        base_dir = normalize_relative_path(uploads_dir)
        if base_dir is None:
            return None, None, "invalid upload path."
        deny_rule = deny_reason(base_dir, deny_globs)
        if deny_rule is not None:
            return None, None, f"path denied by rule: {deny_rule}"
        return base_dir, None, None
    if require_dir or path_value.endswith("/"):
        base_dir = normalize_relative_path(path_value)
        if base_dir is None:
            return None, None, "invalid upload path."
        deny_rule = deny_reason(base_dir, deny_globs)
        if deny_rule is not None:
            return None, None, f"path denied by rule: {deny_rule}"
        if base_dir.is_absolute():
            return None, None, "upload path must be relative."
        return base_dir, None, None
    rel_path = normalize_relative_path(path_value)
    if rel_path is None:
        return None, None, "invalid upload path."
    deny_rule = deny_reason(rel_path, deny_globs)
    if deny_rule is not None:
        return None, None, f"path denied by rule: {deny_rule}"
    return None, rel_path, None


async def handle_file_command(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    message_ts: str | None,
    thread_ts: str | None,
    user_id: str | None,
    args_text: str,
    files: Sequence[SlackFile],
    ambient_context: RunContext | None,
) -> bool:
    reply = make_reply(
        cfg,
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
    )
    if not cfg.files.enabled:
        await reply(
            text="file transfer disabled; enable `[transports.slack.files]`."
        )
        return True

    command, rest, error = parse_file_command(args_text)
    if error is not None:
        await reply(text=error)
        return True
    if command == "put":
        await _handle_file_put(
            cfg,
            reply=reply,
            user_id=user_id,
            args_text=rest,
            files=files,
            ambient_context=ambient_context,
            allow_empty=True,
        )
        return True
    if command == "get":
        await _handle_file_get(
            cfg,
            reply=reply,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
            args_text=rest,
            ambient_context=ambient_context,
        )
        return True

    await reply(text=file_usage())
    return True


async def handle_file_uploads(
    cfg: SlackBridgeConfig,
    *,
    channel_id: str,
    message_ts: str | None,
    thread_ts: str | None,
    user_id: str | None,
    caption_text: str,
    files: Sequence[SlackFile],
    ambient_context: RunContext | None,
) -> bool:
    reply = make_reply(
        cfg,
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
    )
    if not cfg.files.enabled:
        return True
    if not cfg.files.auto_put:
        await reply(text=FILE_PUT_USAGE)
        return True
    caption = caption_text.strip()
    if cfg.files.auto_put_mode == "prompt" and caption:
        await reply(text=FILE_PUT_USAGE)
        return True
    if caption:
        await reply(text=FILE_PUT_USAGE)
        return True

    await _handle_file_put(
        cfg,
        reply=reply,
        user_id=user_id,
        args_text="",
        files=files,
        ambient_context=ambient_context,
        allow_empty=True,
    )
    return True


def _check_permissions(cfg: SlackBridgeConfig, user_id: str | None) -> bool:
    if not cfg.files.allowed_user_ids:
        return True
    if user_id is None:
        return False
    return user_id in cfg.files.allowed_user_ids


async def _handle_file_put(
    cfg: SlackBridgeConfig,
    *,
    reply,
    user_id: str | None,
    args_text: str,
    files: Sequence[SlackFile],
    ambient_context: RunContext | None,
    allow_empty: bool,
) -> None:
    if not files:
        await reply(text=FILE_PUT_USAGE)
        return
    if not _check_permissions(cfg, user_id):
        await reply(text="file transfer is not allowed for this user.")
        return
    try:
        resolved = cfg.runtime.resolve_message(
            text=args_text,
            reply_text=None,
            ambient_context=ambient_context,
        )
    except DirectiveError as exc:
        await reply(text=f"error:\n{exc}")
        return
    try:
        run_root = cfg.runtime.resolve_run_cwd(resolved.context)
    except ConfigError as exc:
        await reply(text=f"error:\n{exc}")
        return
    if run_root is None:
        await reply(text="no project context available for file upload.")
        return
    path_value, force, error = parse_file_prompt(
        resolved.prompt, allow_empty=allow_empty
    )
    if error is not None:
        await reply(text=error)
        return
    require_dir = len(files) > 1
    base_dir, rel_path, path_error = _resolve_put_paths(
        path_value,
        uploads_dir=cfg.files.uploads_dir,
        deny_globs=cfg.files.deny_globs,
        require_dir=require_dir,
    )
    if path_error is not None:
        await reply(text=path_error)
        return
    if require_dir and base_dir is None:
        await reply(text="upload path must be a directory for multiple files.")
        return

    saved: list[FileSaveResult] = []
    failed: list[FileSaveResult] = []
    for file in files:
        result = await _save_slack_file(
            cfg,
            file=file,
            run_root=run_root,
            base_dir=base_dir,
            rel_path=rel_path,
            force=force,
        )
        if result.error is None:
            saved.append(result)
        else:
            failed.append(result)

    context_label = _format_context(cfg, resolved.context)
    if saved:
        total_bytes = sum(item.size or 0 for item in saved)
        if len(saved) == 1:
            rel_path_text = saved[0].rel_path.as_posix() if saved[0].rel_path else ""
            text = (
                f"saved `{rel_path_text}` in {context_label} "
                f"({format_bytes(total_bytes)})"
            )
        else:
            saved_names = ", ".join(f"`{item.name}`" for item in saved)
            base_text = ""
            if base_dir is not None:
                base_text = base_dir.as_posix()
                if not base_text.endswith("/"):
                    base_text = f"{base_text}/"
            text = (
                f"saved {saved_names} to `{base_text}` in {context_label} "
                f"({format_bytes(total_bytes)})"
            )
    else:
        text = "failed to upload files."

    if failed:
        failures = "; ".join(
            f"{item.name}: {item.error}" for item in failed if item.error
        )
        text = f"{text}\n\nfailed: {failures}"

    await reply(text=text)


async def _save_slack_file(
    cfg: SlackBridgeConfig,
    *,
    file: SlackFile,
    run_root: Path,
    base_dir: Path | None,
    rel_path: Path | None,
    force: bool,
) -> FileSaveResult:
    name = file.name or file.file_id
    if file.size is not None and file.size > cfg.files.max_upload_bytes:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error="file is too large to upload.",
        )
    target_rel = rel_path
    if target_rel is None:
        if base_dir is None:
            return FileSaveResult(
                name=name,
                rel_path=None,
                size=None,
                error="missing upload path.",
            )
        filename = default_upload_name(file.name, file.file_id)
        target_rel = base_dir / filename
    deny_rule = deny_reason(target_rel, cfg.files.deny_globs)
    if deny_rule is not None:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error=f"path denied by rule: {deny_rule}",
        )
    target = resolve_path_within_root(run_root, target_rel)
    if target is None:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error="upload path escapes the repo root.",
        )
    if target.exists() and not force:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error="file already exists; pass --force to overwrite.",
        )

    url = file.url_private_download or file.url_private
    if url is None:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error="file has no download url.",
        )
    payload = await cfg.client.download_file(url=url)
    if payload is None:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error="failed to download file.",
        )
    if len(payload) > cfg.files.max_upload_bytes:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error="file is too large to upload.",
        )

    try:
        write_bytes_atomic(target, payload)
    except OSError as exc:
        return FileSaveResult(
            name=name,
            rel_path=None,
            size=None,
            error=f"failed to write file: {exc}",
        )
    return FileSaveResult(
        name=name,
        rel_path=target_rel,
        size=len(payload),
        error=None,
    )


async def _handle_file_get(
    cfg: SlackBridgeConfig,
    *,
    reply,
    channel_id: str,
    thread_ts: str | None,
    user_id: str | None,
    args_text: str,
    ambient_context: RunContext | None,
) -> None:
    if not _check_permissions(cfg, user_id):
        await reply(text="file transfer is not allowed for this user.")
        return
    try:
        resolved = cfg.runtime.resolve_message(
            text=args_text,
            reply_text=None,
            ambient_context=ambient_context,
        )
    except DirectiveError as exc:
        await reply(text=f"error:\n{exc}")
        return
    if resolved.context is None or resolved.context.project is None:
        await reply(text="no project context available for file download.")
        return
    try:
        run_root = cfg.runtime.resolve_run_cwd(resolved.context)
    except ConfigError as exc:
        await reply(text=f"error:\n{exc}")
        return
    if run_root is None:
        await reply(text="no project context available for file download.")
        return
    path_value = resolved.prompt
    if not path_value.strip():
        await reply(text=FILE_GET_USAGE)
        return
    rel_path = normalize_relative_path(path_value)
    if rel_path is None:
        await reply(text="invalid download path.")
        return
    deny_rule = deny_reason(rel_path, cfg.files.deny_globs)
    if deny_rule is not None:
        await reply(text=f"path denied by rule: {deny_rule}")
        return
    target = resolve_path_within_root(run_root, rel_path)
    if target is None:
        await reply(text="download path escapes the repo root.")
        return
    if not target.exists():
        await reply(text="file does not exist.")
        return

    if target.is_dir():
        try:
            payload = zip_directory(
                run_root,
                rel_path,
                cfg.files.deny_globs,
                max_bytes=cfg.files.max_download_bytes,
            )
        except ZipTooLargeError:
            await reply(text="file is too large to send.")
            return
        except OSError as exc:
            await reply(text=f"failed to read directory: {exc}")
            return
        filename = f"{rel_path.name or 'archive'}.zip"
    else:
        try:
            size = target.stat().st_size
            if size > cfg.files.max_download_bytes:
                await reply(text="file is too large to send.")
                return
            payload = target.read_bytes()
        except OSError as exc:
            await reply(text=f"failed to read file: {exc}")
            return
        filename = target.name

    if len(payload) > cfg.files.max_download_bytes:
        await reply(text="file is too large to send.")
        return

    try:
        await cfg.client.upload_file(
            channel_id=channel_id,
            filename=filename,
            content=payload,
            thread_ts=thread_ts,
        )
    except SlackApiError as exc:
        logger.warning("slack.file_upload_failed", error=str(exc))
        await reply(text="failed to send file.")
