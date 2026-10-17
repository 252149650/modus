"""Compiler result and failure contracts shared by providers and consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modus.machine.errors import ProviderError
from modus.machine.facts.build_contracts import BuildContext
from modus.machine.facts.semantic_contracts import CoverageReceipt
from modus.machine.facts.semantic_ir import SemanticFacts


class CompilerSemanticError(ProviderError):
    """A required provider could not produce a current, valid fact set."""

    def __init__(self, _provider_detail: str = "") -> None:
        super().__init__(
            "machine.facts.compiler-provider-failed",
            "compiler provider could not produce a valid fact set",
        )


@dataclass(frozen=True)
class CompilerSemanticResult:
    """One run of compiler-grade semantics across every eligible build unit.

    Provider facts may be reused from the extraction cache, but the result is
    never independently published as a long-lived generation.
    """

    mode: str
    facts: SemanticFacts
    build_context: BuildContext
    provider_status: tuple[dict[str, Any], ...]
    reused_units: tuple[str, ...]
    analyzed_units: tuple[str, ...]
    source_digests: dict[str, str] = field(default_factory=dict)

    @property
    def coverage_receipts(self) -> tuple[CoverageReceipt, ...]:
        """Per-file completeness receipts contributed by every analyzed unit."""
        return self.facts.coverage_receipts

    @property
    def exact_calls(self) -> int:
        """Count of call edges the compiler resolved with exact precision."""
        return sum(
            1
            for edge in self.facts.edges
            if edge.relation == "calls" and edge.resolution == "exact"
        )

    def snapshot(self, root: Path) -> dict[str, Any]:
        """Serialize stable semantics without cache/execution telemetry.

        Whether a build unit was analyzed or reused describes this invocation,
        not the program. Keeping that axis out of the published snapshot makes
        a cold and warm build byte-identical without a pipeline-side rewrite.
        """
        providers = []
        for raw in self.provider_status:
            row = {
                str(key): value
                for key, value in raw.items()
                if key not in {"status", "cacheReusable"}
            }
            if str(row.get("analysisCompleteness") or "") == "complete":
                row["reasonCode"] = "machine.facts.complete"
            providers.append(row)
        return {
            "schemaVersion": 1,
            "mode": self.mode,
            "buildContext": self.build_context.to_dict(root, include_execution=False),
            "providers": providers,
            "analysisContexts": [item.to_dict() for item in self.facts.contexts],
            "coverageReceipts": [item.to_semantic_dict() for item in self.facts.coverage_receipts],
            "observationDispositions": [item.to_dict() for item in self.facts.dispositions],
            "stats": self._snapshot_stats(),
            "coverage": self.build_context.inventory.coverage(include_execution=False),
        }

    def _snapshot_stats(self) -> dict[str, int]:
        """按既有绑定类别计数，不将缓存命中或执行状态写入语义快照。"""
        return {
            "nodes": len(self.facts.nodes),
            "edges": len(self.facts.edges),
            "exactCalls": self.exact_calls,
            "runtimeExactCalls": sum(
                1
                for item in self.facts.edges
                if item.relation == "calls"
                and (item.binding_kind or "declared-exact") == "runtime-exact"
            ),
            "declaredExactCalls": sum(
                1
                for item in self.facts.edges
                if item.relation == "calls" and item.declaration_precision == "exact"
            ),
            "runtimeCandidateCalls": sum(
                1
                for item in self.facts.edges
                if item.relation == "calls" and (item.binding_kind or "") == "runtime-candidate"
            ),
            "unresolvedCalls": sum(
                1
                for item in self.facts.edges
                if item.relation == "calls" and (item.binding_kind or "") == "unresolved"
            ),
            "diagnostics": len(self.facts.diagnostics),
        }
