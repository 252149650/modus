"""Thin command boundary for ``modus context resolve``."""

from __future__ import annotations

from ..config import ModusConfig
from ..orchestrator import context_resolver
from ..paths import ModusPaths
from ..retrieval.budget import RetrievalBudget

__all__ = ["resolve"]


def resolve(
    root: str,
    request: str,
    *,
    scope: str,
    explicit_paths: list[str] | None = None,
    purpose: str | None = None,
    budget: RetrievalBudget | None = None,
) -> dict:
    """Resolve retrieval context, dispatching workspace roots to the aggregator.

    A ``kind: workspace`` config routes across member repositories; anything
    else (including an unreadable config) resolves against the single project.
    """
    paths = ModusPaths.for_root(root)
    try:
        config = ModusConfig.read_available(paths)
    except (FileNotFoundError, OSError, ValueError):
        config = None
    if config is not None and config.kind == "workspace":
        from .. import workspace

        return workspace.resolve_context(
            paths.root,
            request,
            scope=scope,
            explicit_paths=explicit_paths,
            purpose=purpose,
            budget=budget,
        )
    return context_resolver.resolve_context(
        root,
        request,
        scope=scope,
        explicit_paths=explicit_paths,
        purpose=purpose,
        budget=budget,
    )
