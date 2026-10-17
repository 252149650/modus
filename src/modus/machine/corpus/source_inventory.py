"""Repository source inventory shared by structural, compiler and knowledge extraction.

The inventory is deliberately root-aware.  Modus-owned trees are excluded only
when they are direct children of the repository root; a legitimate package such
as ``src/modus`` must never disappear merely because one path segment is named
``modus``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from modus.machine.errors import ContractError
from modus.machine.languages import (
    CapabilityTier,
    capability_for,
    compiler_language,
    compiler_supported,
)
from modus.machine.states import Completeness, ExecutionStatus, SelectionOutcome
from modus.paths import MANAGED_ROOT_FILES, PROJECT_OUTPUT_DIRS

SOURCE_INVENTORY_SCHEMA_VERSION = 1

_ROOT_EXCLUDED_DIRS = frozenset({"modus", ".modus", *PROJECT_OUTPUT_DIRS})


def is_modus_owned_path(root: Path, path: Path) -> bool:
    """Return whether a path is under a repository-top-level Modus output tree."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return True
    return bool(relative.parts) and (
        relative.parts[0] in _ROOT_EXCLUDED_DIRS or relative.as_posix() in MANAGED_ROOT_FILES
    )


def is_modus_owned_relative(value: object) -> bool:
    """Root-relative counterpart of :func:`is_modus_owned_path` without I/O.

    Manifests and ``rg --files`` already yield repository-relative POSIX paths.
    Resolving thousands of those paths during every read-only context lookup is
    both unnecessary and expensive. Traversal/absolute inputs fail closed.
    """
    path = PurePosixPath(str(value or ""))
    return bool(
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] in _ROOT_EXCLUDED_DIRS
        or path.as_posix() in MANAGED_ROOT_FILES
    )


# 文件选择结果、执行状态与完整度是三个独立维度。
# 成功执行不能自动抬升证据层级，缺失原因也不能省略。
@dataclass(frozen=True)
class SourceRecord:
    path: str
    language: str
    selection: SelectionOutcome
    reason_code: str
    capability_tier: CapabilityTier
    structural_extractor: str
    compiler_provider: str = ""
    framework_adapters: tuple[str, ...] = ()
    entry_detectors: tuple[str, ...] = ()
    execution: ExecutionStatus | None = None
    completeness: Completeness = Completeness.FALLBACK

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ContractError("machine.corpus.invalid-inventory-path")
        object.__setattr__(self, "selection", SelectionOutcome(self.selection))
        object.__setattr__(self, "capability_tier", CapabilityTier(self.capability_tier))
        if self.execution is not None:
            object.__setattr__(self, "execution", ExecutionStatus(self.execution))
        object.__setattr__(self, "completeness", Completeness(self.completeness))
        if not self.reason_code:
            raise ContractError("machine.corpus.missing-disposition-reason")

    def to_dict(self, *, include_execution: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.path,
            "language": self.language,
            "selectionOutcome": str(self.selection),
            "capabilityTier": str(self.capability_tier),
            "structuralExtractor": self.structural_extractor,
            "compilerProvider": self.compiler_provider or None,
            "frameworkAdapters": list(self.framework_adapters),
            "entryDetectors": list(self.entry_detectors),
            "analysisCompleteness": str(self.completeness),
            "reasonCode": (
                self.reason_code
                if include_execution or self.completeness is not Completeness.COMPLETE
                else "machine.facts.provider-complete"
            ),
        }
        if include_execution and self.execution is not None:
            result["executionStatus"] = str(self.execution)
        return result


# 以文件、上下文、单元、提供者和阶段组成任务身份。
# 同任务重复回执不能增加分母，互相矛盾的回执降低覆盖结论。
def _context_coverage(
    receipts: tuple[Mapping[str, Any], ...], *, include_execution: bool
) -> tuple[int, dict[str, int], dict[str, dict[str, int]], dict[str, list[str]]]:
    """Fold receipts by task identity before calculating file-level coverage."""
    tasks: dict[tuple[object, ...], list[Mapping[str, Any]]] = {}
    receipt_rows: tuple[Mapping[str, Any], ...] = (
        receipts
        if include_execution
        else tuple(
            {key: value for key, value in row.items() if key != "status"} for row in receipts
        )
    )
    for row in receipt_rows:
        task_key = (
            row.get("sourceFile"),
            row.get("contextId"),
            row.get("buildUnitId"),
            row.get("provider"),
            row.get("phase"),
        )
        tasks.setdefault(task_key, []).append(row)
    # Duplicate observations cannot inflate denominators. Contradictory
    # receipts are partial until a producer emits a consistent task result.
    counts = {"complete": 0, "partial": 0, "failed": 0, "skipped": 0, "unknown": 0}
    by_capability: dict[str, dict[str, int]] = {}
    file_tasks: dict[str, list[str]] = {}
    for group_key, rows in sorted(tasks.items(), key=lambda item: repr(item[0])):
        states = {str(r.get("status") or "complete") for r in rows}
        completeness = {str(r.get("analysisCompleteness") or "unknown") for r in rows}
        state = (
            "complete"
            if completeness == {"complete"} and states <= {"analyzed", "reused", "complete"}
            else "failed"
            if states == {"failed"}
            else "skipped"
            if states <= {"skipped", "not-applicable"}
            else "partial"
            if "partial" in completeness or states & {"analyzed", "reused", "failed", "complete"}
            else "unknown"
        )
        counts[state] += 1
        phase = str(group_key[4] or "unknown")
        phase_counts = by_capability.setdefault(phase, {k: 0 for k in counts})
        phase_counts[state] += 1
        if phase in {"compiler", "analysis", "semantic", "semantic-analysis", "binding"}:
            file_tasks.setdefault(str(group_key[0]), []).append(state)
    return len(tasks), counts, by_capability, file_tasks


# 库存是冻结的选择事实，执行结果通过新值附加而不修改原始选择。
@dataclass(frozen=True)
class SourceInventory:
    records: tuple[SourceRecord, ...]
    context_receipts: tuple[Mapping[str, Any], ...] = ()

    def files_for(self, root: Path, language: str) -> tuple[Path, ...]:
        return tuple(
            root / item.path
            for item in self.records
            if item.language == language and item.selection is SelectionOutcome.INCLUDED
        )

    def with_execution(
        self,
        outcomes: Mapping[str, tuple[ExecutionStatus, Completeness, str]],
    ) -> SourceInventory:
        """Attach N2 outcomes without rewriting the N1 selection decision."""
        records = []
        for item in self.records:
            outcome = outcomes.get(item.path)
            records.append(
                replace(
                    item,
                    execution=outcome[0],
                    completeness=outcome[1],
                    reason_code=outcome[2],
                )
                if outcome is not None
                else item
            )
        return SourceInventory(tuple(records), self.context_receipts)

    def with_receipts(self, receipts: Iterable[Mapping[str, Any] | Any]) -> SourceInventory:
        """Keep task outcomes separate from file classifications and cache hits."""
        normalized: list[Mapping[str, Any]] = []
        for receipt in receipts:
            row = receipt.to_dict() if hasattr(receipt, "to_dict") else dict(receipt)
            normalized.append(row)
        return replace(self, context_receipts=tuple(normalized))

    def coverage(self, *, include_execution: bool = True) -> dict[str, Any]:
        """Report classification and semantic completeness on separate denominators.

        Receipts are grouped by context task before counting. A discovered file or a
        successful execution alone does not prove complete compiler coverage.
        """

        by_language, by_tier = _classification_counts(self.records, include_execution)
        supported = [
            item
            for item in self.records
            if compiler_supported(item.language) and item.selection is SelectionOutcome.INCLUDED
        ]
        task_count, counts, by_capability, file_tasks = _context_coverage(
            self.context_receipts, include_execution=include_execution
        )
        complete_files = [
            item
            for item in supported
            if file_tasks.get(item.path)
            and all(state == "complete" for state in file_tasks[item.path])
        ]
        known_files = [item for item in supported if item.path in file_tasks]
        # Legacy analyzed flags are execution hints, not completeness evidence.
        numerator = len(complete_files)
        denominator = len(supported)
        return {
            "sourceFiles": len(self.records),
            "supportedFiles": denominator,
            "scope": "discovered-non-document-sources",
            "unknownTotal": True,
            "dispositionCoverage": None if not self.records else 1.0,
            "dispositionCoverageMeaning": "classification-of-inventoried-files",
            "compilerCoverage": numerator / denominator if denominator and known_files else None,
            "compilerCoverageNumerator": numerator,
            "compilerCoverageDenominator": denominator,
            "compilerCoverageKnownFiles": len(known_files),
            "compilerCoverageUnknownFiles": denominator - len(known_files),
            "compilerStatus": _compiler_coverage_status(denominator, numerator, len(known_files)),
            "compilerEligibleFiles": denominator,
            "discoveryEligibleFiles": sum(
                item.selection is SelectionOutcome.INCLUDED for item in self.records
            ),
            "structuralEligibleFiles": sum(
                item.selection is SelectionOutcome.INCLUDED for item in self.records
            ),
            "structuralOnlyFiles": sum(
                not compiler_supported(item.language)
                and item.selection is SelectionOutcome.INCLUDED
                for item in self.records
            ),
            "byLanguage": by_language,
            "byCapabilityTier": dict(sorted(by_tier.items())),
            "contextCoverage": {
                "scope": "registered-context-tasks",
                "denominator": task_count,
                "numerator": counts["complete"],
                "ratio": counts["complete"] / task_count if task_count else None,
                "unknownTotal": True,
                "states": counts,
            },
            "capabilityCoverage": by_capability,
        }

    def to_dict(self, *, include_execution: bool = True) -> dict[str, Any]:
        return {
            "schemaVersion": SOURCE_INVENTORY_SCHEMA_VERSION,
            "records": [item.to_dict(include_execution=include_execution) for item in self.records],
            "contextReceipts": (
                [dict(row) for row in self.context_receipts] if include_execution else []
            ),
            "coverage": self.coverage(include_execution=include_execution),
        }


def _classification_counts(
    records: Iterable[SourceRecord], include_execution: bool
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """分类计数独立于上下文完整度，避免把执行成功当作语义覆盖。"""
    by_language: dict[str, dict[str, int]] = {}
    by_tier: dict[str, int] = {}
    for item in records:
        counts = by_language.setdefault(item.language, {})
        coverage_key = (
            str(item.execution or item.selection) if include_execution else str(item.completeness)
        )
        counts[coverage_key] = counts.get(coverage_key, 0) + 1
        tier = str(item.capability_tier)
        by_tier[tier] = by_tier.get(tier, 0) + 1
    return by_language, by_tier


def _compiler_coverage_status(eligible: int, complete: int, known: int) -> str:
    """覆盖状态区分不适用、完整、部分与未知，不将发现文件等同于完成分析。"""
    if not eligible:
        return "not-applicable"
    if complete == eligible:
        return "available"
    return "partial" if known else "unknown"


# 未知语言仍进入库存并标注降级，避免支持率因丢弃未知文件而虚高。
# 文档在专用阅读通道处理，不作为编译器覆盖率分母。
def build_source_inventory(root: Path, *, include_excluded: bool = True) -> SourceInventory:
    """Account for every safely discoverable source, including unknown languages."""
    from modus.machine.corpus.scan import source_files

    del include_excluded
    root = root.resolve()
    records = []
    for path in source_files(root):
        capability = capability_for(path)
        if capability.role == "document":
            continue
        language = compiler_language(capability.language)
        reason = capability.degradation_reason or "machine.facts.compiler-pending"
        records.append(
            SourceRecord(
                path.relative_to(root).as_posix(),
                language,
                SelectionOutcome.INCLUDED,
                reason,
                capability.tier,
                capability.extractor,
                capability.compiler_provider,
                capability.framework_adapters,
                capability.entry_detectors,
            )
        )
    return SourceInventory(tuple(records))


__all__ = [
    "SOURCE_INVENTORY_SCHEMA_VERSION",
    "SourceInventory",
    "SourceRecord",
    "build_source_inventory",
    "is_modus_owned_path",
]
