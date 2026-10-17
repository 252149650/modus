"""Deterministic compiler fact merge and source dispositions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from modus.machine.facts.build_contracts import BuildContext, BuildUnit
from modus.machine.facts.semantic_contracts import AnalysisContext, CoverageReceipt
from modus.machine.facts.semantic_ir import (
    SemanticEdge,
    SemanticFacts,
    SemanticNode,
    canonical_json,
    normalize_facts,
    stable_id,
)
from modus.machine.languages import PROVIDER_VERSIONS
from modus.machine.states import Completeness, ExecutionStatus


# 提供者符号身份可跨上下文稳定，但观察身份必须绑定具体构建上下文。
# 映射节点身份后同步重写边端点和处置记录，保持图引用闭合。
# 保留 providerObservationId，使重复规范化不会不断生成新身份。
def scope_facts(facts: SemanticFacts, unit: BuildUnit | None = None) -> SemanticFacts:
    """Keep provider symbol identity separate from a contextual observation ID."""
    context_id = unit.analysis_context_id if unit else ""
    contexts = list(facts.contexts)
    if unit:
        analysis_context = AnalysisContext(
            context_id,
            unit.id,
            unit.language,
            unit.context_digest,
            PROVIDER_VERSIONS[unit.language],
            {
                key: value
                for key, value in unit.metadata.items()
                if key in {"variantId", "buildTags", "commandEntry"}
            },
        )
        contexts = [analysis_context]

    def context_for(item: SemanticNode | SemanticEdge) -> str:
        return (
            context_id
            or item.analysis_context_id
            or (item.provenance.context_digest if item.provenance else "")
        )

    def original_identity(item: SemanticNode | SemanticEdge) -> str:
        return str(item.attributes.get("providerObservationId") or item.id)

    mapping = {
        (context_for(item), item.id): (
            stable_id("observation", context_for(item), original_identity(item))
            if context_for(item)
            else item.id
        )
        for item in facts.nodes
    }
    nodes = tuple(
        replace(
            item,
            id=mapping[(context_for(item), item.id)],
            symbol_id=item.symbol_id or original_identity(item),
            analysis_context_id=context_for(item),
            attributes={**item.attributes, "providerObservationId": original_identity(item)},
        )
        for item in facts.nodes
    )
    edge_mapping = {}
    edges = []
    for item in facts.edges:
        context = context_for(item)
        identity = (
            stable_id("observation-edge", context, original_identity(item)) if context else item.id
        )
        edge_mapping[(context, item.id)] = identity
        edges.append(
            replace(
                item,
                id=identity,
                analysis_context_id=context,
                attributes={**item.attributes, "providerObservationId": original_identity(item)},
                source=mapping.get((context, item.source), item.source),
                target=mapping.get((context, item.target), item.target),
            )
        )
    dispositions = tuple(
        replace(
            row,
            context_id=context_id or row.context_id,
            record_id=(mapping if row.record_kind == "node" else edge_mapping).get(
                (context_id or row.context_id, row.record_id), row.record_id
            ),
        )
        for row in facts.dispositions
    )
    return replace(
        facts, nodes=nodes, edges=tuple(edges), contexts=tuple(contexts), dispositions=dispositions
    )


# 各构建单元先归一上下文，再做全局规范化与去重。
# 上下文按规范内容排序，提供者完成先后不改变合并结果。
def _merge_facts(items: Iterable[SemanticFacts]) -> SemanticFacts:
    """Merge already-scoped fact sets from every analyzed build unit into one."""
    scoped = tuple(scope_facts(item) for item in items)
    contexts = {canonical_json(row.to_dict()): row for item in scoped for row in item.contexts}
    return normalize_facts(
        SemanticFacts(
            tuple(row for item in scoped for row in item.nodes),
            tuple(row for item in scoped for row in item.edges),
            tuple(row for item in scoped for row in item.diagnostics),
            tuple(contexts[key] for key in sorted(contexts)),
            tuple(row for item in scoped for row in item.dispositions),
            tuple(row for item in scoped for row in item.coverage_receipts),
        )
    )


# 执行状态与完整度独立表达，缓存命中不代表证据更完整。
# 文件解析失败优先于构建单元的总体成功状态。
# 单元级诊断和节点部分完整都会降低对应覆盖收据。
def receipt_facts(
    facts: SemanticFacts, unit: BuildUnit, root: Path, *, status: str, reason: str = ""
) -> SemanticFacts:
    """Report file/capability coverage independently from cache reuse."""
    failures = {
        item.subject: item.code
        for item in facts.diagnostics
        if item.code.endswith(("parse-failed", "translation-unit-failed"))
    }
    partial_unit = any(
        item.severity == "error" or ("diagnostic" in item.code and item.severity == "warning")
        for item in facts.diagnostics
    )
    partial_paths = {
        anchor.file
        for node in facts.nodes
        if node.analysis_completeness != "complete"
        for anchor in node.source_anchors
    }
    receipts = []
    for path in unit.files:
        relative = path.relative_to(root).as_posix()
        failed = failures.get(relative)
        completeness = (
            Completeness.UNAVAILABLE
            if status in {"failed", "skipped"} or failed
            else Completeness.PARTIAL
            if partial_unit or relative in partial_paths
            else Completeness.COMPLETE
        )
        receipts.append(
            CoverageReceipt(
                unit.analysis_context_id,
                unit.id,
                PROVIDER_VERSIONS[unit.language],
                relative,
                "compiler",
                ExecutionStatus.FAILED if failed else ExecutionStatus(status),
                completeness,
                failed or reason,
            )
        )
    return replace(facts, coverage_receipts=tuple(receipts))


# 同一源码可属于多个构建单元，冲突状态按明确优先级合并。
# 失败优先保留，避免另一个单元成功掩盖该文件仍有缺口。
# 原始文件库存保持不变，只附加执行与覆盖信息。
# 文件级诊断在单元状态之后应用，精确失败不会被总体状态覆盖。
# 收据保留完整多上下文证据，供后续阶段解释混合结果。
def _apply_source_dispositions(
    context: BuildContext,
    statuses: Iterable[Mapping[str, Any]],
    facts: Iterable[SemanticFacts] = (),
) -> BuildContext:
    """Record, per source file, whether the compiler analyzed, reused, fell back, or failed.

    Later, stronger dispositions win on conflict (``failed`` beats
    ``analyzed`` beats ``reused`` beats ``fallback``), so one file's status
    always reflects the most decisive evidence available.
    """
    facts = tuple(facts)
    by_unit = {unit.id: unit for unit in context.units}
    values: dict[str, tuple[ExecutionStatus, Completeness, str]] = {}
    priority = {
        ExecutionStatus.SKIPPED: 1,
        ExecutionStatus.REUSED: 2,
        ExecutionStatus.ANALYZED: 3,
        ExecutionStatus.FAILED: 4,
    }

    def assign(
        path: str,
        execution: ExecutionStatus,
        completeness: Completeness,
        reason: str,
    ) -> None:
        current = values.get(path)
        if current is None or priority[execution] >= priority[current[0]]:
            values[path] = (execution, completeness, reason)

    for status in statuses:
        unit = by_unit.get(str(status.get("buildUnitId") or ""))
        if unit is None:
            continue
        execution = ExecutionStatus(str(status.get("status") or ExecutionStatus.SKIPPED))
        completeness = Completeness(
            str(status.get("analysisCompleteness") or Completeness.UNAVAILABLE)
        )
        reason = str(status.get("reasonCode") or f"machine.facts.{execution}")
        for path in unit.files:
            # Inventory paths are already canonical, so match by suffix rather
            # than deriving from a module root that may not be the repository.
            matches = [
                item.path
                for item in context.inventory.records
                if path.as_posix().endswith("/" + item.path) or path.as_posix() == item.path
            ]
            for match in matches:
                assign(match, execution, completeness, reason)
    for item in facts:
        for diagnostic in item.diagnostics:
            if diagnostic.subject and diagnostic.code.endswith(
                ("parse-failed", "translation-unit-failed")
            ):
                assign(
                    diagnostic.subject,
                    ExecutionStatus.FAILED,
                    Completeness.UNAVAILABLE,
                    diagnostic.code,
                )
    inventory = context.inventory.with_execution(values)
    receipts = tuple(receipt for item in facts for receipt in item.coverage_receipts)
    if hasattr(inventory, "with_receipts"):
        inventory = inventory.with_receipts(receipts)
    return replace(context, inventory=inventory)
