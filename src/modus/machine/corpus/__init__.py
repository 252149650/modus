"""N1 Corpus Baseline -- what exactly does this round analyse? Verb: freeze.

This stage freezes inputs, classifies every discovered path exactly once and
issues the round's three-way identity. It never parses symbols and never
draws business conclusions.
"""

from __future__ import annotations

from .contracts import (
    BASELINE_CONTRACT_VERSION,
    BaselineDigests,
    CorpusBaseline,
    InputRecord,
    InputRole,
    RepositoryIdentity,
    analysis_context_digest,
    compose_baseline_digests,
    corpus_digest,
)
from .lifecycle import FrozenCorpus, SealedCorpus, freeze

__all__ = [
    "BASELINE_CONTRACT_VERSION",
    "BaselineDigests",
    "CorpusBaseline",
    "InputRecord",
    "InputRole",
    "RepositoryIdentity",
    "FrozenCorpus",
    "SealedCorpus",
    "analysis_context_digest",
    "compose_baseline_digests",
    "corpus_digest",
    "freeze",
]
