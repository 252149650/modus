"""N2 Program Fact Base -- what was directly observed? Verb: observe.

Parallel producers (structural parser, compiler providers, framework
adapters, rule packs) emit one shared fact envelope. This stage records
observation, never inference: it does not merge look-alike entities and it
does not name modules.
"""

from __future__ import annotations

from .contracts import (
    BoundaryResolver,
    CompilerProvider,
    EntryDetector,
    FactBatch,
    ObservationMethod,
    ProgramFact,
    SourceAnchor,
    StructuralExtractor,
)
from .extension_registry import ExtensionRegistry, RegisteredImplementation, builtin_extensions
from .lifecycle import ObservedProgramFacts, observe

__all__ = [
    "FactBatch",
    "BoundaryResolver",
    "CompilerProvider",
    "EntryDetector",
    "ExtensionRegistry",
    "ObservationMethod",
    "ProgramFact",
    "SourceAnchor",
    "StructuralExtractor",
    "RegisteredImplementation",
    "builtin_extensions",
    "ObservedProgramFacts",
    "observe",
]
