"""Single source of truth for machine-layer state vocabularies and their laws.

The N1-N6 lifecycle uses exactly five orthogonal state axes. They answer
different questions and must never be merged into one enum:

- :class:`SelectionOutcome` -- did this input enter the corpus? (N1)
- :class:`ExecutionStatus`  -- how did a producer run over it? (N2+)
- :class:`Completeness`     -- how much of the expected evidence exists?
- :class:`Resolution`       -- how strongly is a fact or relation bound?
- :class:`ArtifactState`    -- where is a published artifact in its lifecycle?

Two laws hold everywhere and are enforced here, not re-implemented downstream:

1. **Monotone degradation** (boundary doc section 5.5): along any projection
   chain resolution and completeness may stay or weaken, never strengthen.
   Use :func:`resolution_floor` / :func:`completeness_floor` when aggregating.
2. **Legal transitions**: artifact lifecycle moves strictly forward
   (``building -> sealed -> verified -> published``, with ``quarantined`` as
   the failure branch of verification). Use :func:`require_transition`.

No other module may define an inline status-string set; conversions from
foreign vocabularies (for example extraction confidence) must go through
the registered converters below. This invariant is locked by
``tests/machine/test_architecture.py``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from .errors import ContractError, validate_code


class SelectionOutcome(StrEnum):
    """N1 selection axis: exactly one outcome per discovered input."""

    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNSUPPORTED = "unsupported"
    OVERSIZED = "oversized"
    UNAVAILABLE = "unavailable"


class ExecutionStatus(StrEnum):
    """Execution axis: how one producer processed one unit in this round.

    Statuses are immutable records of a finished attempt; a retry is a new
    round with a new record, so this axis has no legal transitions.
    """

    ANALYZED = "analyzed"
    REUSED = "reused"
    SKIPPED = "skipped"
    FAILED = "failed"


class Completeness(StrEnum):
    """Completeness axis: how much of the expected evidence was obtained."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FALLBACK = "fallback"
    UNAVAILABLE = "unavailable"


class Resolution(StrEnum):
    """Resolution axis: how strongly a fact or relation is bound to sources."""

    EXACT = "exact"
    CANDIDATE = "candidate"
    HEURISTIC = "heuristic"
    UNRESOLVED = "unresolved"


class ArtifactState(StrEnum):
    """Artifact lifecycle axis for bundles and published views."""

    BUILDING = "building"
    SEALED = "sealed"
    VERIFIED = "verified"
    QUARANTINED = "quarantined"
    PUBLISHED = "published"


# --- Monotone degradation -------------------------------------------------

#: Strongest first. Aggregations take the weakest member (the "floor").
_RESOLUTION_STRENGTH: Final = (
    Resolution.EXACT,
    Resolution.CANDIDATE,
    Resolution.HEURISTIC,
    Resolution.UNRESOLVED,
)

_COMPLETENESS_STRENGTH: Final = (
    Completeness.COMPLETE,
    Completeness.PARTIAL,
    Completeness.FALLBACK,
    Completeness.UNAVAILABLE,
)


def resolution_floor(first: Resolution | str, *rest: Resolution | str) -> Resolution:
    """Return the weakest resolution among the arguments.

    This is the only sanctioned way to combine resolutions: a capability, a
    proposal or a packet is never more certain than its weakest evidence.
    """
    values = [Resolution(value) for value in (first, *rest)]
    return max(values, key=_RESOLUTION_STRENGTH.index)


def completeness_floor(first: Completeness | str, *rest: Completeness | str) -> Completeness:
    """Return the weakest completeness among the arguments."""
    values = [Completeness(value) for value in (first, *rest)]
    return max(values, key=_COMPLETENESS_STRENGTH.index)


def is_weaker_or_equal(value: Resolution | str, than: Resolution | str) -> bool:
    """Return whether *value* respects monotone degradation relative to *than*."""
    return _RESOLUTION_STRENGTH.index(Resolution(value)) >= _RESOLUTION_STRENGTH.index(
        Resolution(than)
    )


# --- Legal transitions ----------------------------------------------------

_ARTIFACT_TRANSITIONS: Final[dict[ArtifactState, frozenset[ArtifactState]]] = {
    ArtifactState.BUILDING: frozenset({ArtifactState.SEALED}),
    ArtifactState.SEALED: frozenset({ArtifactState.VERIFIED, ArtifactState.QUARANTINED}),
    ArtifactState.VERIFIED: frozenset({ArtifactState.PUBLISHED}),
    ArtifactState.QUARANTINED: frozenset(),
    ArtifactState.PUBLISHED: frozenset(),
}


def can_transition(current: ArtifactState | str, target: ArtifactState | str) -> bool:
    """Return whether the artifact lifecycle allows *current* -> *target*."""
    return ArtifactState(target) in _ARTIFACT_TRANSITIONS[ArtifactState(current)]


def require_transition(current: ArtifactState | str, target: ArtifactState | str) -> None:
    """Raise :class:`ContractError` unless *current* -> *target* is legal."""
    if not can_transition(current, target):
        raise ContractError(
            "machine.store.illegal-artifact-transition",
            f"{ArtifactState(current)} -> {ArtifactState(target)}",
        )


# --- Registered cross-vocabulary conversions --------------------------------

#: 提取置信度在此统一转换为解析精度，避免各观察器自行放大证据强度。
_CONFIDENCE_TO_RESOLUTION: Final = {
    "EXTRACTED": Resolution.EXACT,
    "INFERRED": Resolution.CANDIDATE,
    "AMBIGUOUS": Resolution.CANDIDATE,
}


def resolution_from_confidence(confidence: str) -> Resolution:
    """Convert an extraction-confidence label into the resolution axis.

    Unknown labels degrade to :attr:`Resolution.CANDIDATE` rather than fail:
    a producer's optimism must never strengthen the machine layer's claim.
    """
    return _CONFIDENCE_TO_RESOLUTION.get(confidence, Resolution.CANDIDATE)


# --- Reason codes -----------------------------------------------------------


def validate_reason_code(code: str) -> str:
    """Return *code* when it is a canonical ``machine.<layer>.<kebab>`` reason.

    Reason codes share the namespace of error codes: a degradation reason on a
    coverage receipt and a hard-failure code answer the same question -- "what
    exactly happened" -- with the same stable vocabulary.
    """
    return validate_code(code)


__all__ = [
    "ArtifactState",
    "Completeness",
    "ExecutionStatus",
    "Resolution",
    "SelectionOutcome",
    "can_transition",
    "completeness_floor",
    "is_weaker_or_equal",
    "require_transition",
    "resolution_floor",
    "resolution_from_confidence",
    "validate_reason_code",
]
