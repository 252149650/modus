"""Fail-open runtime orchestration for session-start hooks.

The runtime is intentionally thin: protocol parsing owns untrusted input,
context composition owns project reads, host contracts own output envelopes,
and environment persistence owns its file-system boundary.  This module joins
those pieces and guarantees that no Modus failure can block host startup.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, NoReturn, TextIO, cast

from .context import build_runtime_context
from .contracts import CODEX_ADDITIONAL_CONTEXT_LIMIT, ContextPayload, require_host
from .environment import persist_claude_environment
from .protocol import HookEvent, HookInput, read_hook_event

logger = logging.getLogger(__name__)


def run_hook(
    source: HookInput,
    *,
    root: str | Path,
    platform: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return host-ready startup context, or ``""`` when any step is unavailable."""

    try:
        event = read_hook_event(source, host=platform)
        if event is None:
            return ""
        project_root = _resolve_project_root(root)
        if event.host == "claude":
            persist_claude_environment(event, os.environ if environ is None else environ)
        context = build_runtime_context(project_root)
        return _render_host_output(event, context=context)
    # NOCA:broad-except(宿主启动边界必须放行，内部故障记录日志并返回空上下文)
    except Exception:
        logger.debug("Modus 启动指引不可用", exc_info=True)
        return ""


# Hook 的失败边界保护宿主启动，异常不应阻断用户原有编辑流程。
def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | BinaryIO | None = None,
    stdout: TextIO | BinaryIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Process one stdin event. All failures intentionally return exit code 0."""

    try:
        args = _parser().parse_args(argv)
        input_stream = sys.stdin if stdin is None else stdin
        source = getattr(input_stream, "buffer", input_stream)
        output = run_hook(
            source,
            root=args.root,
            platform=args.platform,
            environ=environ,
        )
        if output:
            _write_stdout(sys.stdout if stdout is None else stdout, output + "\n")
    # NOCA:broad-except(钩子入口故障须返回零退出码保护宿主，日志保留诊断)
    except Exception:
        logger.debug("Modus 钩子启动器失败，已放行当前任务", exc_info=True)
    return 0


class _SilentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _SilentArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--platform", required=True)
    return parser


def _resolve_project_root(root: str | Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise ValueError("Modus 钩子根目录必须为绝对路径")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Modus 钩子根路径必须为目录")
    return resolved


def _render_host_output(event: HookEvent, *, context: str = "") -> str:
    if not context:
        return ""
    payload = require_host(event.host).runtime_payload(context)
    output = _serialize_payload(payload)
    if event.host == "codex":
        # JSON is ASCII-escaped, so this is the exact stdout wire size,
        # including escaping and surrogate-pair expansion for non-BMP text.
        if len(output.encode("utf-8")) > CODEX_ADDITIONAL_CONTEXT_LIMIT:
            return ""
    return output


def _serialize_payload(payload: ContextPayload) -> str:
    return payload if isinstance(payload, str) else _serialize_json(payload)


def _serialize_json(value: Mapping[str, object]) -> str:
    # ASCII JSON avoids the Windows console code page corrupting hook output.
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _write_stdout(stream: TextIO | BinaryIO, text: str) -> None:
    encoded = text.encode("utf-8")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(encoded)
        buffer.flush()
        return
    try:
        cast(TextIO, stream).write(text)
    except TypeError:
        cast(BinaryIO, stream).write(encoded)
    stream.flush()


if __name__ == "__main__":  # pragma: no cover - exercised through the managed launcher
    raise SystemExit(main())
