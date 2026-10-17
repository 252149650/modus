"""A single cooperative deadline shared by every retrieval stage."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .settings import DEFAULT_MAX_CONTEXT_CHARS


@dataclass(frozen=True, slots=True)
class RetrievalBudget:
    """Immutable request budget measured against the monotonic clock.

    Stages receive the same instance instead of creating nested timeouts.  This
    prevents optional fallback work from silently resetting the caller's deadline.
    ``deadline`` and ``started`` intentionally remain public for lightweight
    integrations and deterministic tests.
    """

    started: float
    deadline: float
    max_chars: int = DEFAULT_MAX_CONTEXT_CHARS

    @classmethod
    def start(
        cls, milliseconds: float, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    ) -> RetrievalBudget:
        """Start a budget now; negative durations are treated as exhausted."""

        started = time.monotonic()
        return cls(started, started + max(0.0, milliseconds) / 1000, max_chars)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self.started) * 1000, 3)

    @property
    def duration_ms(self) -> float:
        """Return the configured wall-clock allowance in milliseconds."""

        return round(max(0.0, self.deadline - self.started) * 1000, 3)
