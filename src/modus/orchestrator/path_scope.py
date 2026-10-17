"""Canonical project-path validation for orchestration entry points."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..paths import ModusPaths


class PathOutsideRootError(ValueError):
    """An explicitly selected path resolves outside the project root."""


def relative_paths(
    paths: ModusPaths,
    values: Iterable[str | Path] | None,
) -> list[str]:
    """Normalize paths to unique, root-relative POSIX names.

    Resolution intentionally follows symbolic links before the containment
    check.  A caller therefore cannot smuggle an external file into a
    project-scoped read through a local symlink.
    """
    root = paths.root.resolve()
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        candidate = Path(value)
        target = candidate if candidate.is_absolute() else root / candidate
        try:
            relative = target.resolve().relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise PathOutsideRootError(f"路径位于项目之外： {value}") from exc
        if relative not in seen:
            seen.add(relative)
            normalized.append(relative)
    return normalized
