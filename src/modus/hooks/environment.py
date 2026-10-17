"""Safely persist the minimal session binding required by Claude Code."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from .protocol import HookEvent

MAX_ENVIRONMENT_FILE_BYTES: Final = 1024 * 1024

logger = logging.getLogger(__name__)


def persist_claude_environment(event: HookEvent, environ: Mapping[str, str]) -> None:
    """Best-effort append of canonical binding values to Claude's env file.

    The destination is created by Claude, not Modus.  We reject relative paths,
    symlinks, non-regular files, inode changes, and oversized files.  Values are
    fixed-format ASCII derived from a digest, so no prompt or project content
    can cross this shell-environment boundary.
    """

    try:
        path = _environment_file(environ)
        if path is None:
            return
        _append_missing_bindings(path, event)
    # NOCA:broad-except(可选会话环境持久化失败不能阻塞宿主启动，调试日志保留原因)
    except Exception:
        # Environment persistence is an optional enhancement to startup.
        logger.debug("无法保存 Claude 钩子会话环境", exc_info=True)


def _environment_file(environ: Mapping[str, str]) -> Path | None:
    raw_path = environ.get("CLAUDE_ENV_FILE")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    return path if path.is_absolute() else None


def _append_missing_bindings(path: Path, event: HookEvent) -> None:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return

    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            return
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return
        if after.st_size > MAX_ENVIRONMENT_FILE_BYTES:
            return

        os.lseek(descriptor, 0, os.SEEK_SET)
        existing = os.read(descriptor, after.st_size).decode("utf-8", errors="strict")
        required = (
            f"export MODUS_HOOK_SESSION_KEY={event.session_key}",
            f"export MODUS_HOOK_PLATFORM={event.host}",
        )
        missing = [line for line in required if line not in existing.splitlines()]
        if not missing:
            return

        separator = "" if not existing or existing.endswith("\n") else "\n"
        payload = (separator + "\n".join(missing) + "\n").encode("ascii")
        if after.st_size + len(payload) <= MAX_ENVIRONMENT_FILE_BYTES:
            _write_all(descriptor, payload)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Claude 环境文件写入不完整")
        remaining = remaining[written:]
