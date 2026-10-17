"""N2 output contract: what did producers directly observe in frozen inputs?

Every producer -- structural parser, compiler provider, config parser,
entry detector, boundary resolver, authoring rule pack -- emits the same envelope so N3 can
consume one vocabulary. ``ProgramFact`` is a *logical* contract: aggregate
facts materialise as instances, while occurrence-level observations stay in
compact per-file pages and are exposed through the same field names.

A fact records observation, never inference: ``resolution == exact`` means
"precisely observed", not "true at runtime", and a heuristic match stays
``heuristic`` no matter how confident the producer feels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from modus.config import ModusConfig
    from modus.paths import ModusPaths

    from ..corpus.source_snapshot import SourcePath
    from .build_contracts import BuildUnit
    from .semantic_ir import SemanticFacts

from ..receipts import CoverageReceipt
from ..states import Completeness, Resolution


class ObservationMethod(StrEnum):
    """How a producer arrived at a fact."""

    PARSED = "parsed"
    COMPILED = "compiled"
    DECLARED = "declared"
    MATCHED = "matched"


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """Where in the frozen corpus a fact was observed."""

    path: str
    source_digest: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sourceDigest": self.source_digest,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class ProgramFact:
    """One directly observed fact bound to frozen evidence.

    ``subject`` and ``target`` are producer-scoped identifiers; identity
    unification across producers is N3's job, never N2's.
    """

    fact_id: str
    fact_kind: str
    subject: str
    anchor: SourceAnchor
    producer: str
    producer_version: str
    observation_method: ObservationMethod
    resolution: Resolution
    completeness: Completeness
    baseline_digest: str
    analysis_context_id: str = ""
    target: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": "program-fact",
            "factId": self.fact_id,
            "factKind": self.fact_kind,
            "subject": self.subject,
            "target": self.target,
            "anchor": self.anchor.to_dict(),
            "producer": self.producer,
            "producerVersion": self.producer_version,
            "observationMethod": str(self.observation_method),
            "resolution": str(self.resolution),
            "completeness": str(self.completeness),
            "baselineDigest": self.baseline_digest,
            "analysisContextId": self.analysis_context_id,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class FactBatch:
    """Everything one producer emitted for one round: facts plus receipts.

    ``payload`` carries the producer's compact physical representation (for
    example the extraction dict feeding the evidence graph); it is an internal
    optimisation and never crosses the machine boundary.
    """

    producer: str
    facts: tuple[ProgramFact, ...] = ()
    receipts: tuple[CoverageReceipt, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    payload: Any = None


@runtime_checkable
class StructuralExtractor(Protocol):
    """Parse one frozen source without project-wide inference."""

    # 双下划线参数保留位置参数协议，也兼容旧版静态分析器的语法解析。
    def __call__(self, __path: SourcePath) -> dict[str, Any]:
        """从单个冻结源码提取可序列化的结构观察。"""


@runtime_checkable
class CompilerProvider(Protocol):
    """Produce T1 semantic observations for one resolved build unit."""

    def __call__(
        self, __paths: ModusPaths, __config: ModusConfig, __unit: BuildUnit
    ) -> SemanticFacts:
        """结合已解析构建上下文生成编译器语义证据。"""


@runtime_checkable
class EntryDetector(Protocol):
    """Promote directly observed framework declarations to entry evidence."""

    def __call__(
        self,
        __relative: str,
        __content: str,
        __language: str,
        __extraction: dict[str, Any],
    ) -> dict[str, Any]:
        """使用源码与既有提取结果识别框架入口。"""


@runtime_checkable
class BoundaryResolver(Protocol):
    """Close source-local framework boundaries without semantic inference."""

    def __call__(self, __source: SourcePath, __facts: dict[str, Any]) -> dict[str, Any]:
        """在当前源码边界内补充可直接观察的框架关系。"""


__all__ = [
    "FactBatch",
    "BoundaryResolver",
    "CompilerProvider",
    "EntryDetector",
    "ObservationMethod",
    "ProgramFact",
    "SourceAnchor",
    "StructuralExtractor",
]
