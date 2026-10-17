"""Shared topology identities and provenance primitives.

This module owns the small, stable vocabulary used by topology assembly,
method-graph projection, and validation. It must not perform orchestration or
read repository state.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from modus.machine.facts.semantic_ir import SemanticNode

TOPOLOGY_SCHEMA_VERSION: Final = 1

_RESOLUTION_RANK: Final = {
    "exact": 3,
    "candidate": 2,
    "heuristic": 1,
    "unresolved": 0,
}


def resolution_rank(value: str) -> int:
    """Return a total ordering for precision-floor calculations."""

    return _RESOLUTION_RANK.get(value, 0)


def semantic_owner(node: SemanticNode) -> str:
    """Return the language-qualified owner recorded on a semantic node."""

    value = str(node.attributes.get("owner") or "")
    if value:
        return f"{node.language}:{value}"
    if "#" in node.qualified_name:
        return f"{node.language}:{node.qualified_name.split('#', 1)[0]}"
    return ""


def derivation(
    rule: str,
    premises: Iterable[str],
    context: str = "syntax",
) -> dict[str, Any]:
    """Build one deterministic topology provenance step."""

    return {
        "rule": rule,
        "producer": "topology",
        "contextId": context,
        "premises": sorted(set(premises)),
    }


__all__ = ["TOPOLOGY_SCHEMA_VERSION", "derivation", "resolution_rank", "semantic_owner"]
