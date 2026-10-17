"""Bind registry capabilities to the structural extractors that fulfil them.

The pure language registry lives in :mod:`modus.machine.languages`; this
module contributes the N2-only step of resolving an ``extractor`` name to a
callable. Unknown extractor names fall back to bounded generic-text evidence,
so a registry entry can never make discovery fail.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache

from modus.machine.facts.contracts import StructuralExtractor
from modus.machine.languages import (
    PROVIDER_VERSIONS,
    LanguageCapability,
    capability_for,
    capability_registry,
    compiler_language,
    compiler_supported,
    discovery_suffixes,
)


@lru_cache(maxsize=1)
def structural_extractors() -> Mapping[str, StructuralExtractor]:
    """Map each registered suffix to its extraction callable."""
    from modus.machine.facts.extension_registry import builtin_extensions

    implementations = builtin_extensions().structural_extractors
    return {
        suffix: implementations[row["extractor"]].implementation
        for suffix, row in capability_registry().items()
    }


__all__ = [
    "LanguageCapability",
    "PROVIDER_VERSIONS",
    "capability_for",
    "capability_registry",
    "compiler_language",
    "compiler_supported",
    "discovery_suffixes",
    "structural_extractors",
]
