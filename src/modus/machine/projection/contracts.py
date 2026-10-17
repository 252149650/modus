"""N5 output contract: which authoring decisions do the facts support?

A projection organises N3/N4 evidence into proposals an author can judge --
it never asserts business truth. Every proposal traces back to capabilities;
structural community and directory layout are auxiliary signals with lower
weight than entries, authoritative objects, states and effects.

Oversized proposals fail structurally (``needs-split``) instead of paginating
forever, and capabilities that fit nowhere land in an explicit remainder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import ContractError
from ..receipts import CoverageReceipt


class ProjectionIntent(StrEnum):
    """The authoring question a projection answers."""

    SCOPE = "scope"
    SERVICES = "services"
    EVIDENCE = "evidence"
    CONSTRAINTS = "constraints"
    REINIT_DIFF = "reinit-diff"
    COVERAGE = "coverage"


class ProposalDisposition(StrEnum):
    """Machine judgement of a proposal's structural health."""

    PROPOSED = "proposed"
    NEEDS_SPLIT = "needs-split"
    REMAINDER = "remainder"
    SHARED_INFRASTRUCTURE = "shared-infrastructure"


@dataclass(frozen=True, slots=True)
class Proposal:
    """One evidence-backed module/domain candidate built from capabilities."""

    proposal_id: str
    kind: str
    capability_ids: tuple[str, ...]
    disposition: ProposalDisposition = ProposalDisposition.PROPOSED
    representative_entries: tuple[dict[str, Any], ...] = ()
    authoritative_objects: tuple[str, ...] = ()
    terminal_effects: tuple[dict[str, Any], ...] = ()
    supporting_signals: dict[str, Any] = field(default_factory=dict)
    ambiguities: tuple[dict[str, Any], ...] = ()
    receipts: tuple[CoverageReceipt, ...] = ()
    omitted_counts: dict[str, int] = field(default_factory=dict)
    expansion_handles: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.capability_ids and self.disposition not in {
            ProposalDisposition.REMAINDER,
            ProposalDisposition.SHARED_INFRASTRUCTURE,
        }:
            raise ContractError(
                "machine.projection.proposal-without-capability",
                f"proposal {self.proposal_id} carries no behaviour evidence",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": "proposal",
            "proposalId": self.proposal_id,
            "kind": self.kind,
            "capabilityIds": list(self.capability_ids),
            "disposition": str(self.disposition),
            "representativeEntries": [dict(row) for row in self.representative_entries],
            "authoritativeObjects": list(self.authoritative_objects),
            "terminalEffects": [dict(row) for row in self.terminal_effects],
            "supportingSignals": dict(self.supporting_signals),
            "ambiguities": [dict(row) for row in self.ambiguities],
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "omittedCounts": dict(self.omitted_counts),
            "expansionHandles": [dict(row) for row in self.expansion_handles],
        }


@dataclass(frozen=True, slots=True)
class AuthoringProjection:
    """One intent-scoped view derived from the resolved and capability models."""

    intent: ProjectionIntent
    baseline_digest: str
    proposals: tuple[Proposal, ...] = ()
    records: tuple[dict[str, Any], ...] = ()
    receipts: tuple[CoverageReceipt, ...] = ()
    omitted_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": "authoring-projection",
            "intent": str(self.intent),
            "baselineDigest": self.baseline_digest,
            "proposals": [proposal.to_dict() for proposal in self.proposals],
            "records": [dict(row) for row in self.records],
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "omittedCounts": dict(self.omitted_counts),
        }


__all__ = [
    "AuthoringProjection",
    "Proposal",
    "ProposalDisposition",
    "ProjectionIntent",
]
