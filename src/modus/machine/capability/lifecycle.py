"""Compose entry-driven behavior slices from the resolved program.

The current composer boundary is intentionally explicit even while richer
entry-to-effect traversal is added behind it: N5 consumes CapabilityModel,
never the raw N3 graph by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..resolution.lifecycle import ResolvedProgram
from ..states import Completeness, ExecutionStatus
from .composer import compose_capabilities
from .contracts import CapabilityModel


@dataclass(frozen=True, slots=True)
class ComposedCapabilities:
    """N4-owned behavior model bound to one baseline identity."""

    baseline_digest: str
    model: CapabilityModel
    stage_summary: dict[str, Any]


def compose(resolved: ResolvedProgram) -> ComposedCapabilities:
    """Return the bounded behavior model consumed by projections.

    Every slice is deterministic and source-bound; an uncovered or truncated
    seed stays visible in the model instead of being silently discarded.
    """
    model = compose_capabilities(resolved)
    completeness = (
        Completeness.COMPLETE
        if model.seed_count and not model.uncovered_seeds
        else Completeness.PARTIAL
        if model.seed_count
        else Completeness.UNAVAILABLE
    )
    return ComposedCapabilities(
        resolved.baseline_digest,
        model,
        {
            "status": str(ExecutionStatus.ANALYZED),
            "completeness": str(completeness),
            "seeds": model.seed_count,
            "capabilities": len(model.slices),
            "uncoveredSeeds": len(model.uncovered_seeds),
        },
    )


__all__ = ["ComposedCapabilities", "compose"]
