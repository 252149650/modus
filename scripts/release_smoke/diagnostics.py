"""Small diagnostic primitives shared by the release gate."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import ReleaseCheckError


def require(condition: object, message: str) -> None:
    """Raise an actionable contract error when ``condition`` is false."""

    if not condition:
        raise ReleaseCheckError(message)


def format_paths(paths: Iterable[str]) -> str:
    """Render paths deterministically for stable local and CI diagnostics."""

    return "\n".join(f"  - {path}" for path in sorted(paths))
