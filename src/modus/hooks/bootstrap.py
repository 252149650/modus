"""Render install-time hook assets and native host configuration.

This module is the installation facade used by platform adapters.  Host wire
contracts live in :mod:`modus.hooks.contracts`; the functions here only compose
those contracts into deterministic files that can be tracked by the manifest.
"""

from __future__ import annotations

import json
import shlex
from typing import Final

from .context import injection_text
from .contracts import CODEX_ADDITIONAL_CONTEXT_LIMIT, SUPPORTED_HOSTS, require_host

HOOKS_REL: Final = ".modus/hooks"
# Stable generation order shared by both launchers.
PLATFORMS: Final = SUPPORTED_HOSTS


def hook_command(platform: str) -> str:
    """Return the command a platform hook config invokes at session start."""

    host = require_host(platform).name
    return f"{HOOKS_REL}/run-hook.cmd session-start {host}"


def runtime_hook_command(platform: str) -> str:
    """Return the Python runtime command used by a session-start hook."""

    host = require_host(platform).name
    if host == "claude":
        # Claude documents this variable for project-scoped absolute hook paths.
        # Quoting keeps worktree paths with spaces intact on supported hosts.
        return '"${CLAUDE_PROJECT_DIR}/.modus/hooks/run-hook.cmd" runtime --platform claude'
    if host == "codex":
        # Codex runs command hooks with the session cwd. Resolve from the Git
        # root so starting Codex in a repository subdirectory remains stable.
        return (
            '"$(git rev-parse --show-toplevel)/.modus/hooks/run-hook.cmd" runtime --platform codex'
        )
    return f"{HOOKS_REL}/run-hook.cmd runtime --platform {host}"


def runtime_hook_command_windows(platform: str) -> str:
    """Return the Windows override for hosts with an explicit command field."""

    host = require_host(platform).name
    if host != "codex":
        return runtime_hook_command(host)
    return (
        'powershell.exe -NoProfile -NonInteractive -Command "$root = '
        "git rev-parse --show-toplevel; if ($LASTEXITCODE -eq 0) { & "
        "(Join-Path $root '.modus/hooks/run-hook.cmd') runtime --platform codex }\""
    )


def _static_payload(platform: str) -> str:
    payload = require_host(platform).static_payload(injection_text())
    if isinstance(payload, str):  # Defensive: no static contract currently emits plain text.
        return payload
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _batch_literal(value: str) -> str:
    # Values are generated at install time, never taken from a host event.
    escaped = value.replace("^", "^^").replace("%", "%%")
    for character in "&|<>()":
        escaped = escaped.replace(character, "^" + character)
    return escaped


def run_hook_cmd() -> str:
    """Render one fixed-action launcher understood by cmd.exe and POSIX sh.

    The first line is a batch label and a complete POSIX dispatch program. It
    exits before the shell encounters the batch program. Static guidance is
    pre-serialized, so Windows needs neither Bash nor Python to show it.
    """
    return _posix_dispatcher() + "\n" + "\n".join(_windows_dispatcher()) + "\n"


def _posix_dispatcher() -> str:
    return (
        ':; modus_hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0; case '
        '"${1-}" in session-start) shift; exec sh "$modus_hook_dir/session-start" "$@";; '
        "runtime) shift; for modus_hook_python in python3 python; do if "
        '"$modus_hook_python" -c "import modus.hooks.runtime" >/dev/null 2>&1; then exec '
        '"$modus_hook_python" "$modus_hook_dir/modus-hook.py" "$@"; fi; done; printf '
        '"%s\\n" "[modus] 启动指引不可用：缺少 Python 运行环境" >&2; exit 0;; *) exit 0;; esac'
    )


def _windows_dispatcher() -> list[str]:
    batch = [
        "@echo off",
        "setlocal EnableExtensions DisableDelayedExpansion",
        'if /I "%~1"=="session-start" goto modus_static',
        'if /I not "%~1"=="runtime" exit /b 0',
        'py -3 -c "import modus.hooks.runtime" >nul 2>nul',
        "if not errorlevel 1 goto modus_python_launcher",
        'python -c "import modus.hooks.runtime" >nul 2>nul',
        "if errorlevel 1 goto modus_unavailable",
        'python "%~dp0modus-hook.py" %*',
        "exit /b 0",
        ":modus_python_launcher",
        'py -3 "%~dp0modus-hook.py" %*',
        "exit /b 0",
        ":modus_unavailable",
        "echo [modus] 启动指引不可用：缺少 Python 运行环境 1>&2",
        "exit /b 0",
        ":modus_static",
    ]
    for platform in PLATFORMS:
        batch.extend([f'if /I "%~2"=="{platform}" goto modus_static_{platform}'])
    batch.append("goto modus_static_codex")
    for platform in PLATFORMS:
        batch.extend(
            [
                f":modus_static_{platform}",
                "echo " + _batch_literal(_static_payload(platform)),
                "exit /b 0",
            ]
        )
    return batch


def runtime_launcher() -> str:
    """Render the location-bound runtime entry; arbitrary scripts are not accepted."""
    return """#!/usr/bin/env python3
import sys
from pathlib import Path


def launch() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["runtime"]:
        arguments = arguments[1:]
    try:
        from modus.hooks.runtime import main
        return main(["--root", str(Path(__file__).resolve().parents[2]), *arguments])
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(launch())
"""


def session_start_script() -> str:
    """Serialize at installation; the launcher only selects a host envelope."""
    lines = ["#!/bin/sh", 'case "${1:-codex}" in']
    for platform in PLATFORMS:
        lines.append(f"  {platform}) printf '%s\\n' {shlex.quote(_static_payload(platform))} ;;")
    lines.append(f"  *) printf '%s\\n' {shlex.quote(_static_payload('codex'))} ;;")
    return "\n".join([*lines, "esac", "exit 0", ""])


def claude_hook_config() -> dict[str, object]:
    """Claude Code session-start hook merged into ``.claude/settings.json``."""

    command = runtime_hook_command("claude")
    hook = {"type": "command", "command": command, "async": False}
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact|fork",
                    "hooks": [hook],
                }
            ],
        }
    }


def cursor_hook_config() -> dict[str, object]:
    """Cursor project hook config (``.cursor/hooks.json``)."""
    return {
        "version": 1,
        "hooks": {"sessionStart": [{"command": hook_command("cursor")}]},
    }


def codex_hook_config() -> dict[str, object]:
    """Codex session-start hook for the authoritative ``.codex/hooks.json`` layer."""

    base_hook = {
        "type": "command",
        "command": runtime_hook_command("codex"),
        "commandWindows": runtime_hook_command_windows("codex"),
        "timeout": 10,
    }
    context_hook = {
        **base_hook,
        # Covers the managed dispatcher, a worst-case 4 KiB recovery capsule,
        # and both envelopes. Runtime enforces the same byte ceiling and keeps
        # the capsule if a user-edited dispatcher grows beyond the budget.
        "additionalContextLimit": CODEX_ADDITIONAL_CONTEXT_LIMIT,
    }
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [context_hook],
                }
            ],
        }
    }


def codebuddy_hook_config() -> dict[str, object]:
    """CodeBuddy project hook config (``.codebuddy/settings.json``)."""
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command("codebuddy"),
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    }
