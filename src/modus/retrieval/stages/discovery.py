"""Safely discover Markdown knowledge without following filesystem links."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ...paths import ModusPaths


def discover_markdown_files(
    paths: ModusPaths,
    kind: str,
    *,
    scan_issues: list[dict[str, Any]] | None = None,
    deadline_monotonic: float | None = None,
) -> list[Path]:
    """Return sorted regular Markdown files beneath the configured knowledge root.

    Symlinks are rejected at both directory and file boundaries.  Problems are
    reported as structured issues so callers can distinguish "no knowledge" from
    a partial or unsafe scan.  ``kind`` is retained for the public stage contract;
    both project and workspace roots currently expose ``paths.knowledge_dir``.
    """

    del kind
    root = paths.knowledge_dir
    issues = scan_issues if scan_issues is not None else []
    if root.is_symlink():
        _record_unsafe_path(issues, paths, root, reason="unsafe-directory")
        return []
    if not root.is_dir():
        if root.exists():
            _record_unsafe_path(issues, paths, root, reason="unsafe-directory")
        return []

    files: list[Path] = []

    def record_walk_error(error: OSError) -> None:
        issues.append(
            {
                "type": "knowledge-read",
                "path": str(error.filename),
                "reason": "unreadable-directory",
            }
        )

    for current, directories, names in os.walk(root, followlinks=False, onerror=record_walk_error):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            issues.append({"type": "knowledge-budget", "reason": "discovery-deadline"})
            break
        current_path = Path(current)
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink():
                _record_unsafe_path(issues, paths, candidate, reason="unsafe-directory")
        directories[:] = sorted(
            name for name in directories if not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            target = current_path / name
            if target.suffix.lower() != ".md":
                continue
            if target.is_symlink() or not target.is_file():
                _record_unsafe_path(issues, paths, target, reason="unsafe-file")
                continue
            # ``relative`` also enforces the configured root boundary.
            paths.relative(target)
            files.append(target)
    return files


# 记录被拒绝的路径而非静默跳过，让调用者区分空知识与不完整扫描。
def _record_unsafe_path(
    issues: list[dict[str, Any]],
    paths: ModusPaths,
    target: Path,
    *,
    reason: str,
) -> None:
    issues.append(
        {
            "type": "knowledge-read",
            "path": str(target.relative_to(paths.root)),
            "reason": reason,
        }
    )


__all__ = ["discover_markdown_files"]
