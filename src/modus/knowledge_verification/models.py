"""Public result vocabulary for authored-knowledge integrity checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    """One deterministic defect tied to a repository-relative document."""

    document: str
    code: str
    detail: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable CLI wire shape."""

        return {
            "document": self.document,
            "finding": self.code,
            "severity": "error",
            "detail": self.detail,
            **self.context,
        }


def finding(document: str, code: str, detail: str, **context: Any) -> VerificationFinding:
    """Create a finding without leaking result-shape assembly across checks."""

    return VerificationFinding(document, code, detail, context)


__all__ = ["VerificationFinding", "finding"]
