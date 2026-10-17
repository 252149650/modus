"""N3 output contract: who are the entities, and which relations hold?

N3 unifies multi-producer observations into canonical entities and typed
relations while preserving every source binding. Name similarity alone may
produce a *candidate*, never an identity; uncertainty may stay or fall,
never rise (see :func:`modus.machine.states.resolution_floor`).

The full navigation graph is not a published dataset. It is an on-demand,
budgeted traversal capability over the resolved model -- expressed here as
:class:`TraversalQuery` -- so downstream layers pull the few paths they need
instead of receiving every edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import BudgetError
from ..states import Resolution


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """One canonical entity with every source observation that supports it."""

    entity_id: str
    kind: str
    name: str
    language: str = ""
    resolution: Resolution = Resolution.EXACT
    bindings: tuple[dict[str, Any], ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedRelation:
    """One typed relation with occurrence witnesses and a resolution floor."""

    relation_id: str
    kind: str
    source_id: str
    target_id: str
    resolution: Resolution
    witnesses: tuple[dict[str, Any], ...] = ()
    analysis_context_id: str = ""


class TraversalDirection(StrEnum):
    FORWARD = "forward"
    BACKWARD = "backward"


@dataclass(frozen=True, slots=True)
class TraversalBudget:
    """Hard bounds for one traversal; exhaustion truncates, never fails."""

    max_nodes: int = 2_000
    max_edges: int = 8_000
    max_depth: int = 32
    max_paths: int = 64

    def __post_init__(self) -> None:
        if min(self.max_nodes, self.max_edges, self.max_depth, self.max_paths) < 1:
            raise BudgetError(
                "machine.resolution.invalid-traversal-budget",
                "every traversal bound must be positive",
            )


@dataclass(frozen=True, slots=True)
class TraversalQuery:
    """A directed, typed, budgeted walk -- the only sanctioned graph access."""

    seeds: tuple[str, ...]
    direction: TraversalDirection = TraversalDirection.FORWARD
    relation_kinds: tuple[str, ...] = ()
    budget: TraversalBudget = TraversalBudget()

    def __post_init__(self) -> None:
        if not self.seeds:
            raise BudgetError(
                "machine.resolution.traversal-without-seeds",
                "a traversal must start from at least one entity",
            )


__all__ = [
    "ResolvedEntity",
    "ResolvedRelation",
    "TraversalBudget",
    "TraversalDirection",
    "TraversalQuery",
]
