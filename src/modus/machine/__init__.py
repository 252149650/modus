"""Public façade for the Modus machine layer.

The machine layer turns a frozen repository into a bounded, trustworthy
context packet for knowledge authoring. It is organised as six semantic
stages, each a subpackage whose public API is a single verb:

- :mod:`~modus.machine.corpus`     -- ``freeze``   -- N1 Corpus Baseline
- :mod:`~modus.machine.facts`      -- ``observe``  -- N2 Program Fact Base
- :mod:`~modus.machine.resolution` -- ``resolve``  -- N3 Resolved Program Model
- :mod:`~modus.machine.capability` -- ``compose``  -- N4 Capability Model
- :mod:`~modus.machine.projection` -- ``project``  -- N5 Authoring Projections
- :mod:`~modus.machine.packet`     -- ``package``  -- N6 Authoring Context Packet

Dependencies point strictly backwards (packet -> ... -> corpus); the
cross-cutting planes :mod:`~modus.machine.store` (persistence, integrity) and
:mod:`~modus.machine.runtime` (resource policy) are used by stages and depend
on none of them. Design rationale lives in
``docs/knowledge-lifecycle-boundary.md``.

Everything the machine layer produces is verifiable, rebuildable and
disposable; authoritative project knowledge exists only in the authored
knowledge base downstream.
"""

from __future__ import annotations

from .api import (
    analyze_repository,
    build_authoring_context,
    open_analysis,
    query_evidence,
)
from .errors import (
    BudgetError,
    ConfigurationError,
    ContractError,
    DriftError,
    IntegrityError,
    InvariantError,
    MachineError,
    ProviderError,
)
from .packet.contracts import (
    AUTHORING_CONTEXT_SCHEMA_VERSION,
    AuthoringContextPacket,
    ContextRequest,
    PacketBudget,
    PacketScope,
    TaskIntent,
)
from .store.contracts import (
    AnalysisBundle,
    AnalysisIdentity,
    AnalysisOptions,
    AnalysisReceipt,
    EvidenceBudget,
    EvidencePage,
    EvidenceQuery,
)

__all__ = [
    "AUTHORING_CONTEXT_SCHEMA_VERSION",
    "AnalysisBundle",
    "AnalysisIdentity",
    "BudgetError",
    "AnalysisOptions",
    "AnalysisReceipt",
    "AuthoringContextPacket",
    "ConfigurationError",
    "ContextRequest",
    "ContractError",
    "DriftError",
    "EvidenceBudget",
    "EvidenceQuery",
    "EvidencePage",
    "IntegrityError",
    "InvariantError",
    "MachineError",
    "PacketBudget",
    "PacketScope",
    "ProviderError",
    "TaskIntent",
    "analyze_repository",
    "build_authoring_context",
    "open_analysis",
    "query_evidence",
]
