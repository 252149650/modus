"""Immutable contracts shared by build-context resolvers and compiler providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from modus.machine.corpus.source_inventory import SourceInventory
from modus.machine.facts.semantic_ir import SemanticDiagnostic, stable_id

BUILD_CONTEXT_SCHEMA_VERSION: Final = 1


def repository_relative(root: Path, value: Path) -> str:
    """Render a stable repository-relative coordinate when one exists."""

    try:
        return value.resolve().relative_to(root.resolve()).as_posix() or "."
    except (OSError, ValueError):
        return value.as_posix()


@dataclass(frozen=True)
class Toolchain:
    """One detected local compiler/type-checker and why it is usable."""

    language: str
    executable: str
    version: str
    available: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "executable": self.executable,
            "version": self.version,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BuildUnit:
    """A reproducible repository slice analyzed as one semantic context.

    The unit ID names the selected action; context_digest binds its current
    inputs. Consumers must retain both dimensions when comparing observations.
    """

    id: str
    language: str
    root: Path
    files: tuple[Path, ...]
    context_digest: str
    build_system: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def analysis_context_id(self) -> str:
        return stable_id("analysis-context", self.language, self.id)

    def to_dict(self, repository_root: Path) -> dict[str, Any]:
        return {
            "id": self.id,
            "analysisContextId": self.analysis_context_id,
            "language": self.language,
            "root": repository_relative(repository_root, self.root),
            "files": [repository_relative(repository_root, item) for item in self.files],
            "contextDigest": self.context_digest,
            "buildSystem": self.build_system,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BuildContext:
    """Build units, toolchains, diagnostics, and source inventory."""

    units: tuple[BuildUnit, ...]
    toolchains: tuple[Toolchain, ...]
    diagnostics: tuple[SemanticDiagnostic, ...]
    inventory: SourceInventory = field(default_factory=lambda: SourceInventory(()))

    def to_dict(self, repository_root: Path, *, include_execution: bool = True) -> dict[str, Any]:
        """Serialize discovery and optionally include execution accounting.

        Wire keys and unit order are shared with providers and persisted receipts;
        internal naming changes must not alter this representation.
        """

        return {
            "schemaVersion": BUILD_CONTEXT_SCHEMA_VERSION,
            "units": [item.to_dict(repository_root) for item in self.units],
            "toolchains": [item.to_dict() for item in self.toolchains],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "sourceInventory": self.inventory.to_dict(include_execution=include_execution),
        }


__all__ = [
    "BUILD_CONTEXT_SCHEMA_VERSION",
    "BuildContext",
    "BuildUnit",
    "Toolchain",
    "repository_relative",
]
