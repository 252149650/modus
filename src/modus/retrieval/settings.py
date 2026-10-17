"""Typed access to the tunable limits of the retrieval pipeline."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..config import ModusConfig
from ..paths import ModusPaths

DEFAULT_COLD_BUDGET_MS = 3_000.0
DEFAULT_MAX_CONTEXT_CHARS = 12_000
DEFAULT_MAX_MEMBERS = 3
DEFAULT_MAX_CANDIDATES = 24
DEFAULT_MAX_BODY_SECTIONS = 6


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    """Validated view of retrieval limits used by the execution pipeline.

    Configuration loading remains separate from consumers so a future caller can
    construct these limits directly (for example, for an embedded integration or
    a deterministic benchmark) without manufacturing a full ``ModusConfig``.
    """

    cold_budget_ms: float = DEFAULT_COLD_BUDGET_MS
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    max_members: int = DEFAULT_MAX_MEMBERS
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_body_sections: int = DEFAULT_MAX_BODY_SECTIONS

    def __post_init__(self) -> None:
        if (
            isinstance(self.cold_budget_ms, bool)
            or not isinstance(self.cold_budget_ms, (int, float))
            or not math.isfinite(self.cold_budget_ms)
            or self.cold_budget_ms <= 0
        ):
            raise ValueError("cold_budget_ms 必须为大于零的有限数值")
        for name in (
            "max_context_chars",
            "max_members",
            "max_candidates",
            "max_body_sections",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为大于零的整数")

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> RetrievalSettings:
        return cls(
            cold_budget_ms=values.get("cold_budget_ms", DEFAULT_COLD_BUDGET_MS),
            max_context_chars=values.get("max_context_chars", DEFAULT_MAX_CONTEXT_CHARS),
            max_members=values.get("max_members", DEFAULT_MAX_MEMBERS),
            max_candidates=values.get("max_candidates", DEFAULT_MAX_CANDIDATES),
            max_body_sections=values.get("max_body_sections", DEFAULT_MAX_BODY_SECTIONS),
        )

    @classmethod
    def read(cls, paths: ModusPaths) -> RetrievalSettings:
        return cls.from_mapping(retrieval_settings(paths))


def retrieval_settings(paths: ModusPaths) -> dict[str, Any]:
    """Return a copy of raw settings for backward-compatible integrations."""

    return dict(ModusConfig.read_available(paths).heuristic_grounding)


_settings = retrieval_settings

__all__ = ["RetrievalSettings", "retrieval_settings"]
