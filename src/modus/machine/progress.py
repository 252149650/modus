"""Stable progress events for long-running machine lifecycle work.

Progress is an observational channel: it may explain where the pipeline is and
how much bounded work has completed, but it must never influence evidence,
bundle identity, or error semantics.  The public callback remains a plain text
sink for compatibility; this module owns the structured event model and its
single, machine-readable rendering.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Final, TypeAlias

Progress: TypeAlias = Callable[[str], None]
Clock: TypeAlias = Callable[[], float]

_STAGE_PATTERN: Final = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
_UNIT_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]*\Z")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One safe progress observation, independent of pipeline semantics."""

    stage: str
    completed: int | None = None
    total: int | None = None
    unit: str = "items"
    elapsed_ms: float | None = None

    def __post_init__(self) -> None:
        if not _STAGE_PATTERN.fullmatch(self.stage):
            raise ValueError("progress stage must be a lowercase dotted identifier")
        if self.completed is not None and self.completed < 0:
            raise ValueError("completed progress must be non-negative")
        if self.total is not None and self.total < 0:
            raise ValueError("total progress must be non-negative")
        if self.total is not None and self.completed is None:
            raise ValueError("total progress requires a completed value")
        if not _UNIT_PATTERN.fullmatch(self.unit):
            raise ValueError("progress unit must be a lowercase identifier")
        if self.elapsed_ms is not None and self.elapsed_ms < 0:
            raise ValueError("elapsed progress time must be non-negative")

    def render(self) -> str:
        """Render a compact line whose keys remain stable for agents and logs."""

        fields = [self.stage]
        if self.completed is not None:
            progress = (
                str(self.completed) if self.total is None else f"{self.completed}/{self.total}"
            )
            fields.extend((f"progress={progress}", f"unit={self.unit}"))
        if self.elapsed_ms is not None:
            fields.append(f"elapsedMs={self.elapsed_ms:.0f}")
        return " ".join(fields)


class ProgressReporter:
    """Emit throttled progress while retaining non-semantic phase timings.

    A reporter owns one active phase at a time. Transitions are always visible;
    repeated work updates are throttled by wall time and may be forced at a
    natural boundary. The resulting timings are diagnostics only and never
    participate in a content digest.
    """

    def __init__(
        self,
        sink: Progress | None = None,
        *,
        min_update_interval: float = 15.0,
        clock: Clock = perf_counter,
    ) -> None:
        if min_update_interval < 0:
            raise ValueError("progress update interval must be non-negative")
        self._sink = sink
        self._clock = clock
        self._min_update_interval = min_update_interval
        self._active_stage: str | None = None
        self._stage_started = 0.0
        self._last_update = 0.0
        self._timings: dict[str, float] = {}

    @property
    def emits_updates(self) -> bool:
        """Whether work loops should spend effort producing progress samples."""

        return self._sink is not None

    def transition(self, stage: str) -> None:
        """Close the current phase and announce the next one immediately."""

        now = self._clock()
        self._close_active(now)
        event = ProgressEvent(stage)
        self._active_stage = stage
        self._stage_started = now
        self._last_update = now
        if self._sink is not None:
            self._sink(event.render())

    def advance(
        self,
        completed: int,
        total: int | None = None,
        *,
        unit: str = "items",
        force: bool = False,
    ) -> None:
        """Report forward work for the active phase without flooding stderr."""

        if self._active_stage is None:
            raise RuntimeError("cannot advance progress without an active stage")
        if self._sink is None:
            return
        now = self._clock()
        if not force and now - self._last_update < self._min_update_interval:
            return
        event = ProgressEvent(
            self._active_stage,
            completed=completed,
            total=total,
            unit=unit,
            elapsed_ms=(now - self._stage_started) * 1000,
        )
        self._last_update = now
        self._sink(event.render())

    def finish(self) -> None:
        """Close the active phase; safe to call from a ``finally`` block."""

        self._close_active(self._clock())
        self._active_stage = None

    def timings(self) -> Mapping[str, float]:
        """Return completed phase durations in milliseconds."""

        return dict(self._timings)

    def _close_active(self, now: float) -> None:
        if self._active_stage is None:
            return
        elapsed_ms = (now - self._stage_started) * 1000
        self._timings[self._active_stage] = self._timings.get(self._active_stage, 0.0) + elapsed_ms


__all__ = ["Progress", "ProgressEvent", "ProgressReporter"]
