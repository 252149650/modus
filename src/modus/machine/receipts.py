"""One coverage-receipt shape shared by every machine layer.

A coverage receipt states, for one scope (a file, a build unit, an analysis
context, a traversal), how a producer executed and how complete the resulting
evidence is. Receipts are how degradation stays visible: "stage ran" and
"evidence is complete" are different axes and a receipt carries both.

Receipts flow upward unmodified: a packet can always answer "why is this file
only structurally analysed" by quoting the N2 receipt verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .states import Completeness, ExecutionStatus, validate_reason_code


@dataclass(frozen=True, slots=True)
class CoverageReceipt:
    """Execution and completeness for exactly one scope of one producer."""

    producer: str
    scope_kind: str
    scope_id: str
    status: ExecutionStatus
    completeness: Completeness
    reason_code: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.reason_code:
            validate_reason_code(self.reason_code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": "coverage-receipt",
            "producer": self.producer,
            "scopeKind": self.scope_kind,
            "scopeId": self.scope_id,
            "status": str(self.status),
            "completeness": str(self.completeness),
            "reasonCode": self.reason_code,
            "counts": dict(self.counts),
        }


__all__ = ["CoverageReceipt"]
