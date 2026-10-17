"""N1 output contract: what exactly does this analysis round claim to cover?

A :class:`CorpusBaseline` is the boundary contract, identity and coverage
receipt for one round of analysis. It never contains ASTs, symbols or
business conclusions -- those belong to N2 and later.

Identity is split in three digests so cache invalidation can distinguish
"the corpus changed" from "the analysis environment changed":

- ``corpus_digest``            -- paths, roles and content of included inputs
- ``analysis_context_digest``  -- configuration, toolchains, build context
- ``baseline_digest``          -- both digests plus the contract version
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from ..errors import ContractError
from ..states import SelectionOutcome

#: Version of the baseline contract itself; bump on incompatible change.
BASELINE_CONTRACT_VERSION: Final = 1


class InputRole(StrEnum):
    """Why an input participates in the corpus."""

    SOURCE = "source"
    TEST = "test"
    CONFIGURATION = "configuration"
    SCHEMA = "schema"
    DOCUMENT = "document"
    GENERATED = "generated"
    EXISTING_KNOWLEDGE = "existing-knowledge"
    USER_MATERIAL = "user-material"


@dataclass(frozen=True, slots=True)
class InputRecord:
    """Exactly one selection outcome for one discovered input."""

    path: str
    role: InputRole
    outcome: SelectionOutcome
    reason_code: str = ""
    content_digest: str = ""
    size_bytes: int = 0

    def __post_init__(self) -> None:
        if self.outcome is SelectionOutcome.INCLUDED and not self.content_digest:
            raise ContractError(
                "machine.corpus.included-without-digest",
                f"included input requires a content digest: {self.path}",
            )
        if self.outcome is not SelectionOutcome.INCLUDED and not self.reason_code:
            raise ContractError(
                "machine.corpus.outcome-without-reason",
                f"{self.outcome} requires a stable reason code: {self.path}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": str(self.role),
            "outcome": str(self.outcome),
            "reasonCode": self.reason_code,
            "contentDigest": self.content_digest,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Stable identity of the analysed working tree."""

    revision: str = ""
    dirty: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "dirty": self.dirty}


@dataclass(frozen=True, slots=True)
class BaselineDigests:
    """The three-way identity of one analysis round."""

    corpus_digest: str
    analysis_context_digest: str
    baseline_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "corpusDigest": self.corpus_digest,
            "analysisContextDigest": self.analysis_context_digest,
            "baselineDigest": self.baseline_digest,
        }


def _canonical_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def corpus_digest(inputs: tuple[InputRecord, ...]) -> str:
    """Digest of what the corpus *is*: included paths, roles and content."""
    payload = [
        [record.path, str(record.role), record.content_digest]
        for record in sorted(inputs, key=lambda record: record.path)
        if record.outcome is SelectionOutcome.INCLUDED
    ]
    return _canonical_digest({"contract": "machine-corpus", "inputs": payload})


def analysis_context_digest(context: dict[str, Any]) -> str:
    """Digest of the environment the corpus is analysed *in*."""
    return _canonical_digest({"contract": "machine-analysis-context", "context": context})


def compose_baseline_digests(
    inputs: tuple[InputRecord, ...], context: dict[str, Any]
) -> BaselineDigests:
    """Compute the three-way baseline identity in one deterministic pass."""
    corpus = corpus_digest(inputs)
    environment = analysis_context_digest(context)
    baseline = _canonical_digest(
        {
            "contract": "machine-baseline",
            "contractVersion": BASELINE_CONTRACT_VERSION,
            "corpusDigest": corpus,
            "analysisContextDigest": environment,
        }
    )
    return BaselineDigests(corpus, environment, baseline)


@dataclass(frozen=True, slots=True)
class CorpusBaseline:
    """The complete N1 product: boundary contract plus coverage accounting.

    Invariant: every discovered input appears exactly once in ``inputs`` and
    ``accounted inputs / discovered inputs == 100%``; silence is a bug.
    """

    repository: RepositoryIdentity
    inputs: tuple[InputRecord, ...]
    digests: BaselineDigests
    analysis_config: dict[str, Any] = field(default_factory=dict)
    toolchains: tuple[dict[str, Any], ...] = ()

    def outcome_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.inputs:
            counts[str(record.outcome)] = counts.get(str(record.outcome), 0) + 1
        return counts

    def included(self) -> tuple[InputRecord, ...]:
        return tuple(
            record for record in self.inputs if record.outcome is SelectionOutcome.INCLUDED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": "corpus-baseline",
            "contractVersion": BASELINE_CONTRACT_VERSION,
            "repository": self.repository.to_dict(),
            "digests": self.digests.to_dict(),
            "inputs": [record.to_dict() for record in self.inputs],
            "outcomeCounts": self.outcome_counts(),
            "analysisConfig": dict(self.analysis_config),
            "toolchains": [dict(row) for row in self.toolchains],
        }


__all__ = [
    "BASELINE_CONTRACT_VERSION",
    "BaselineDigests",
    "CorpusBaseline",
    "InputRecord",
    "InputRole",
    "RepositoryIdentity",
    "analysis_context_digest",
    "compose_baseline_digests",
    "corpus_digest",
]
