"""Compose the small, trusted context injected at session start."""

from __future__ import annotations

from pathlib import Path

from ..activation_guidance import MODUS_ENTRY_HINT

__all__ = ["build_runtime_context", "injection_text"]


def injection_text() -> str:
    """Return applicability guidance without eagerly loading project knowledge."""

    return f"<MODUS_CONTEXT>\n{MODUS_ENTRY_HINT}\n</MODUS_CONTEXT>"


def build_runtime_context(_project_root: Path) -> str:
    """Return the stable, always-on session-start guidance.

    ``_project_root`` is kept for a stable call signature and possible future
    project-scoped guidance; nothing here reads it today.

    By design this never imports or reads initialization analysis. A
    session-start hook fires on ordinary work and must depend only on the
    stable authored-knowledge surface.
    """

    return injection_text()
