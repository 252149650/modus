"""Final, context-aware dispositions of observed reference occurrences.

Graph edges are observations of an occurrence, not independent denominator
items. A compiler binding can settle an earlier structural attempt; incomplete
location information remains visible instead of being silently guessed away.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from modus.machine.corpus.source_coordinates import SourceCoordinates
from modus.machine.facts.entry_observation import source_location

REFERENCE_OUTCOMES_SCHEMA_VERSION = 1

Location = dict[str, Any]
Span = tuple[str, str, int, int]
GroupKey = tuple[object, ...]
GroupRow = dict[str, Any]


# 统一路径分隔符，同时保留仓库外绝对路径的外部身份。
def _path(value: str, root: Path | None = None) -> str:
    if root and Path(value).is_absolute():
        try:
            return Path(value).relative_to(root).as_posix()
        except ValueError:
            return value
    return value.replace("\\", "/")


# 名称尾部只用于检索展示，不参与引用发生位置的身份判定。
def _tail(value: str) -> str:
    return value.rsplit(".", 1)[-1].rsplit("::", 1)[-1].removesuffix("()")


# 有发生 ID 时优先使用显式身份，否则使用位置与分析上下文组合。
# 上下文始终参与分组，避免不同构建配置相互覆盖。
def _key(location: Location, context: str, occurrence: str = "") -> GroupKey:
    return (
        ("id", occurrence, context)
        if occurrence
        else (
            location["path"],
            location["startLine"],
            location["startColumn"],
            location.get("endLine"),
            location.get("endColumn"),
            context,
        )
    )


# 缺少原始 ID 时从规范分组键生成稳定摘要，不把源码文本嵌入键中。
def _identity(key: GroupKey) -> str:
    return (
        "reference:"
        + hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()[:32]
    )


# 统计单位是源码引用发生位置，多个解析尝试可归入同一记录。
# 结构观察为编译器上下文提供支持，不另算成冲突目标。
# 位置不足的引用仍保留未知原因，不能靠相似名称强行合并。
def reconcile_reference_outcomes(
    extraction: Mapping[str, Any],
    graph: Any,
    *,
    baseline_digest: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Group every observed reference occurrence and settle its final disposition.

    Structural attempts and compiler bindings for the same source span are
    merged into one occurrence record, so the unresolved-reference report
    counts real source positions rather than duplicate graph edges.
    """
    groups: dict[GroupKey, GroupRow] = {}
    locations = SourceCoordinates(root)
    raw_values = (
        extraction.get("referenceOutcomes")
        or extraction.get("references")
        or extraction.get("raw_calls")
        or extraction.get("unresolved_refs")
        or []
    )
    raw_refs = (
        [row for row in raw_values if isinstance(row, Mapping)]
        if isinstance(raw_values, (list, tuple))
        else []
    )
    # 先建立原生发生 ID 与字节范围索引，再处理图中的增强观察。
    prepared: list[tuple[int, Mapping[str, Any], Location, Span | None, str]] = []
    native_by_span: defaultdict[Span, set[str]] = defaultdict(set)
    for position, ref in enumerate(raw_refs):
        relative = _path(str(ref.get("callerFile") or ref.get("source_file") or ""), root)
        adjusted = dict(ref)
        if not adjusted.get("source_location") and ref.get("line"):
            adjusted["source_location"] = f"L{ref['line']}"
        location = source_location(relative, adjusted)
        location["path"] = _path(location["path"], root)
        span = locations.span(location)
        occurrence = str(ref.get("occurrenceId") or ref.get("occurrence_id") or "")
        if span and occurrence:
            native_by_span[span].add(occurrence)
        prepared.append((position, ref, location, span, occurrence))
    edge_iterator = (
        graph.edges(keys=True, data=True)
        if graph.is_multigraph()
        else ((a, b, "", data) for a, b, data in graph.edges(data=True))
    )
    for source, target, edge_key, attrs in edge_iterator:
        if attrs.get("relation") not in {"calls", "references"}:
            continue
        relative = _path(
            str(attrs.get("source_file") or graph.nodes[source].get("source_file") or ""), root
        )
        location = source_location(relative, attrs)
        location["path"] = _path(location["path"], root)
        span = locations.span(location)
        context = str(
            attrs.get("context_id")
            or attrs.get("analysisContextId")
            or attrs.get("analysis_context_id")
            or ""
        )
        occurrence = str(attrs.get("occurrenceId") or attrs.get("occurrence_id") or "")
        correspondence = None
        native_matches = native_by_span.get(span, set()) if span else set()
        # 仅在范围唯一对应时补充发生 ID，多义范围保持独立。
        if not occurrence and span is not None and len(native_matches) == 1:
            occurrence = next(iter(native_matches))
            correspondence = {
                "occurrenceId": occurrence,
                "method": "same-source-byte-range",
                "sourceDigest": span[1],
            }
        key = _key(location, context, occurrence)
        if not occurrence and span:
            key = (*span, context)
        # 没有可定位发生位置时加入端点与边键，防止无关未知引用被折叠。
        if location["startLine"] is None and not occurrence:
            key += (str(source), str(target), str(edge_key))
        target_attrs = graph.nodes[target]
        name = str(
            attrs.get("callee")
            or attrs.get("operation")
            or target_attrs.get("label")
            or target_attrs.get("qualified_name")
            or target
        )
        row = groups.setdefault(
            key,
            {
                "occurrenceId": occurrence or _identity(key),
                "location": location,
                "contextId": context,
                "name": name,
                "caller": str(source),
                "attempts": [],
                "observations": [],
                "candidates": [],
                "span": span,
            },
        )
        observation = str(
            attrs.get("semantic_edge_id")
            or attrs.get("id")
            or edge_key
            or _identity(key + (str(target),))
        )
        # 每次图观察保留绑定等级与来源，最终状态在收集完成后统一决定。
        row["observations"].append(
            {
                "id": observation,
                "target": str(target),
                "resolution": attrs.get("resolution", "candidate"),
                "bindingKind": attrs.get("binding_kind", attrs.get("bindingKind", "structural")),
                "resolvedBy": attrs.get("resolvedBy", "structural"),
                "analysisCompleteness": attrs.get(
                    "analysis_completeness", attrs.get("analysisCompleteness", "structural")
                ),
                "declarationResolution": attrs.get(
                    "declaration_resolution", attrs.get("declarationResolution")
                ),
                "dispatchResolution": attrs.get(
                    "dispatch_resolution", attrs.get("dispatchResolution")
                ),
                "provenance": attrs.get("semantic_provenance") or attrs.get("provenance") or {},
                **({"occurrenceCorrespondence": correspondence} if correspondence else {}),
            }
        )
        row["candidates"].append(str(target))
    # A syntax observation can support several registered compiler contexts.
    # Preserve it as a correspondence, not a conflicting exact compiler target.
    by_occurrence: defaultdict[str, list[GroupKey]] = defaultdict(list)
    for key, row in groups.items():
        by_occurrence[row["occurrenceId"]].append(key)
    for keys in by_occurrence.values():
        contextual = [
            key
            for key in keys
            if groups[key]["contextId"]
            and any(o["resolvedBy"] == "compiler" for o in groups[key]["observations"])
        ]
        if not contextual:
            continue
        for key in keys:
            row = groups[key]
            if row["contextId"] or any(o["resolvedBy"] == "compiler" for o in row["observations"]):
                continue
            for destination in contextual:
                groups[destination].setdefault("supportingObservations", []).extend(
                    row["observations"]
                )
            del groups[key]
    # 建立反向索引供结构尝试归并，避免重复扫描全部分组。
    occurrence_groups: defaultdict[str, list[GroupRow]] = defaultdict(list)
    span_groups: defaultdict[Span, list[GroupRow]] = defaultdict(list)
    for row in groups.values():
        occurrence_groups[row["occurrenceId"]].append(row)
        if row["span"]:
            span_groups[row["span"]].append(row)
    for position, ref, location, span, occurrence in prepared:
        name = str(ref.get("fullName") or ref.get("name") or ref.get("callee") or "")
        if not name:
            continue
        context = str(ref.get("contextId") or ref.get("context_id") or "")
        matches = (
            occurrence_groups.get(occurrence, [])
            if occurrence
            else span_groups.get(span, [])
            if span
            else []
        )
        # 有明确上下文的原始引用只能附着到同一上下文结果。
        if context:
            matches = [row for row in matches if row["contextId"] == context]
        # 未找到对应观察也保留原始尝试，确保未解析项不会从分母消失。
        if not matches:
            key = _key(location, context, occurrence) + (position, name)
            row = {
                "occurrenceId": occurrence or _identity(key),
                "location": location,
                "contextId": context,
                "name": name,
                "caller": str(ref.get("caller_nid") or ref.get("caller") or ""),
                "attempts": [],
                "observations": [],
                "candidates": list(ref.get("candidates") or []),
                "reason": str(ref.get("reason") or "no-declaration")
                if span or occurrence
                else "insufficient-occurrence-location",
            }
            groups[key] = row
            matches = [row]
        for row in matches:
            row["attempts"].append(
                {
                    "stage": str(ref.get("stage") or "structure"),
                    "reason": str(ref.get("reason") or "unbound"),
                    "resolver": str(ref.get("resolvedBy") or "structural"),
                    "status": str(ref.get("status") or "unresolved"),
                }
            )
    outcomes, unresolved = [], []
    for row in groups.values():
        # 字节范围只供内部归并使用，不泄露为未声明的输出字段。
        row.pop("span", None)
        observations = row["observations"]
        exact = [o for o in observations if o["resolution"] == "exact"]
        # 多条精确观察若指向同一目标仍算已绑定；不同目标则属于冲突。
        exact_targets = {o["target"] for o in exact}
        if len(exact_targets) == 1:
            row["status"], row["reason"] = "bound", "machine.resolution.exact-binding"
        elif len(exact_targets) > 1:
            row["status"], row["reason"] = (
                "candidate",
                "machine.resolution.conflicting-exact-bindings",
            )
        elif observations:
            row["status"], row["reason"] = "candidate", "machine.resolution.candidate-bindings"
        else:
            row["status"] = "unresolved"
            row.setdefault("reason", "no-declaration")
        row["candidates"] = sorted(set(str(v) for v in row["candidates"]))
        # 按观察身份去重排序，稳定报告顺序而不重复计算同一事实。
        row["observations"] = sorted(
            {o["id"]: o for o in observations}.values(), key=lambda o: o["id"]
        )
        row["callerFile"] = row["location"]["path"]
        row["line"] = row["location"]["startLine"]
        row["nameTail"] = _tail(row["name"])
        row["candidateCount"] = len(row["candidates"])
        # 简版未解析列表限制候选展示量，完整结果仍保留全部候选。
        if row["status"] == "unresolved":
            unresolved.append(
                {
                    **row,
                    "candidates": row["candidates"][:8],
                    "candidatesTruncated": len(row["candidates"]) > 8,
                }
            )
        outcomes.append(row)
    # 排序优先源码位置，便于维护者从报告直接逐项核查文件。
    outcomes.sort(
        key=lambda row: (
            row["callerFile"],
            row["line"] or 0,
            row["location"]["startColumn"] or 0,
            row["contextId"],
            row["occurrenceId"],
        )
    )
    # 状态计数从最终发生记录生成，避免重复图边放大统计。
    states = {
        state: sum(row["status"] == state for row in outcomes)
        for state in ("bound", "candidate", "unresolved", "excluded")
    }
    # 只有明确完整枚举才能给出分母和解析比例，部分观察不能冒充全量覆盖。
    complete = extraction.get("referenceEnumerationComplete") is True
    total = len(outcomes)
    return {
        "schemaVersion": REFERENCE_OUTCOMES_SCHEMA_VERSION,
        "baselineDigest": baseline_digest,
        "refs": unresolved,
        "outcomes": outcomes,
        "stats": {
            reason: sum(row["reason"] == reason for row in unresolved)
            for reason in sorted(
                {
                    "member-call",
                    "no-declaration",
                    "ambiguous",
                    *(row["reason"] for row in unresolved),
                }
            )
        },
        "coverage": {
            "scope": "observed-contextual-reference-occurrences",
            "knownCount": total,
            "denominator": total if complete else None,
            "unknownTotal": not complete,
            "sampled": bool(extraction.get("referencesSampled")),
            "states": states,
            "resolvedRatio": states["bound"] / total if complete and total else None,
        },
    }
