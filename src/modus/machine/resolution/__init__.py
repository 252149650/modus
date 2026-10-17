"""N3 Resolved Program Model -- who is who, which relations hold? Verb: resolve.

This stage unifies identities across producers, binds references and keeps
every uncertainty explicit. It never substitutes structural communities for
behaviour, and its graph is traversed on demand, never published wholesale.
"""

from __future__ import annotations

from .contracts import (
    ResolvedEntity,
    ResolvedRelation,
    TraversalBudget,
    TraversalDirection,
    TraversalQuery,
)
from .lifecycle import ResolvedProgram, resolve

__all__ = [
    "ResolvedEntity",
    "ResolvedRelation",
    "TraversalBudget",
    "TraversalDirection",
    "TraversalQuery",
    "ResolvedProgram",
    "resolve",
]
