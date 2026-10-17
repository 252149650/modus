"""Canonical topology records and bounded aggregation of call occurrences.

Each occurrence retains its own source witness and identity before aggregation.
Summary edges preserve bounded representative evidence and report ambiguity;
aggregation must not turn a heuristic classification into an exact observation.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from modus.machine.facts.semantic_ir import SemanticEdge, stable_id
from modus.machine.resolution.topology_model import (
    resolution_rank as _resolution_rank,
)
from modus.machine.resolution.topology_rules import (
    _strongest_resolution,
)

# Aggregated edges keep a bounded sample of witnesses to stay readable.
_MAX_REPRESENTATIVE_CALL_SITES = 5
_MAX_AGGREGATED_EVIDENCE = 8


# 统一拓扑节点的公共字段，并复制输入容器，隔离调用方后续修改。
def _topology_node(
    *,
    node_id: str,
    kind: str,
    qualified_name: str,
    build_unit_id: str,
    language: str = "generic",
    source_anchors: Iterable[Mapping[str, Any]] = (),
    attributes: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "language": language,
        "qualifiedName": qualified_name,
        "buildUnitId": build_unit_id,
        "sourceAnchors": [dict(item) for item in source_anchors],
        "attributes": dict(attributes or {}),
        "provenance": dict(provenance or {}),
    }


# 摘要边身份绑定端点、关系、契约及调用位置，重复构建可稳定对齐。
def _topology_edge(
    *,
    source: str,
    target: str,
    relation: str,
    resolution: str,
    operation: str = "",
    contract_key: str = "",
    evidence: Iterable[str] = (),
    call_site: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    edge_id = stable_id(
        "topology-edge",
        source,
        target,
        relation,
        contract_key,
        json.dumps(call_site or {}, sort_keys=True),
    )
    value = {
        "id": edge_id,
        "from": source,
        "to": target,
        "relation": relation,
        "resolution": resolution,
        "operation": operation,
        "contractKey": contract_key,
        "evidence": list(evidence),
        "attributes": dict(attributes or {}),
        "provenance": dict(provenance or {}),
    }
    if call_site:
        value["callSite"] = dict(call_site)
    return value


# 发生记录额外绑定操作和原始语义边，保留逐次调用的可寻址证据。
# 聚合只生成视图，不替代这些原始见证。
def _occurrence_edge(
    *,
    source: str,
    target: str,
    relation: str,
    resolution: str,
    operation: str = "",
    contract_key: str = "",
    evidence: Iterable[str] = (),
    call_site: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One addressable observation; aggregation never discards this record."""
    witness_id = stable_id(
        "topology-occurrence",
        source,
        target,
        relation,
        operation,
        contract_key,
        json.dumps(call_site or {}, sort_keys=True),
        str((attributes or {}).get("functionEdgeId") or ""),
    )
    return {
        "id": witness_id,
        "from": source,
        "to": target,
        "relation": relation,
        "resolution": resolution,
        "operation": operation,
        "contractKey": contract_key,
        "evidence": list(evidence),
        "attributes": dict(attributes or {}),
        "provenance": dict(provenance or {}),
        "callSite": dict(call_site) if call_site else None,
    }


# 代表位置按文件、行和列排序，选择不依赖输入遍历顺序。
def _call_site_sort_key(site: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(site.get("file") or ""),
        int(site.get("line") or 0),
        int(site.get("column") or 0),
    )


# 聚合表达至少存在一个相应等级的见证，而非所有调用都同样可靠。
# 最强见证同时提供解析等级、绑定属性和来源，避免拼接出不存在的证据。
# 展示样本有上限，完整见证 ID 与省略计数仍保留。
def _finalize_occurrence_edges(
    raw_edges: Iterable[Mapping[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Summarize existential evidence without claiming universal confidence.

    The best witness owns resolution, binding and provenance together. Coverage
    counts describe all observations separately; paths must use occurrences,
    never these independently aggregated existential statements.
    """
    groups: dict[tuple[str, str, str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for raw in raw_edges:
        site = raw.get("callSite")
        file = str(site.get("file") or "") if isinstance(site, Mapping) else ""
        # 分组包含源码文件与操作，同端点的不同操作不会被混为一条语义。
        key = (
            str(raw["from"]),
            str(raw["to"]),
            str(raw["relation"]),
            str(raw.get("contractKey") or ""),
            str(raw.get("operation") or ""),
            file,
        )
        groups[key].append(raw)

    finalized: dict[str, dict[str, Any]] = {}
    for (source, target, relation, contract_key, operation, _file), occurrences in groups.items():
        occurrences = list(
            {
                str(
                    item.get("id") or stable_id("occurrence", json.dumps(item, sort_keys=True))
                ): item
                for item in occurrences
            }.values()
        )
        resolution = _strongest_resolution(
            str(item.get("resolution") or "unresolved") for item in occurrences
        )
        # 分类依据不一致需要诊断，即使某次调用拥有较强的解析结果。
        classification_tiers = sorted(
            {
                str((item.get("attributes") or {}).get("classificationEvidence") or "")
                for item in occurrences
            }
            - {""}
        )
        if len(classification_tiers) > 1:
            diagnostics.append(
                {
                    "recordKind": "diagnostic",
                    "code": "machine.resolution.mixed-classification",
                    "message": (
                        f"{source} -> {target} ({relation}) has inconsistent classification "
                        f"evidence across occurrences: {classification_tiers}"
                    ),
                    "severity": "warning",
                    "provider": "topology-aggregation",
                    "buildUnitId": "",
                    "subject": f"{source}|{target}|{relation}",
                }
            )
        bindings = sorted(
            {str((item.get("attributes") or {}).get("bindingKind") or "") for item in occurrences}
            - {""}
        )
        call_sites: list[dict[str, Any]] = []
        # 同一位置只占一个代表样本，避免重复观察挤占其他源码位置。
        seen_sites: set[tuple[Any, ...]] = set()
        ordered_occurrences = sorted(
            occurrences,
            key=lambda item: _call_site_sort_key(item.get("callSite") or {}),
        )
        for item in ordered_occurrences:
            site = item.get("callSite")
            if not isinstance(site, Mapping):
                continue
            token = _call_site_sort_key(site)
            if token in seen_sites:
                continue
            seen_sites.add(token)
            call_sites.append(dict(site))
            if len(call_sites) >= _MAX_REPRESENTATIVE_CALL_SITES:
                break
        evidence: list[str] = []
        for item in occurrences:
            for value in item.get("evidence") or ():
                if value not in evidence:
                    evidence.append(value)
        # 证据文本排序后再截断，使样本不受提供者枚举次序影响。
        evidence.sort()
        # 等级相同时按稳定 ID 选择代表，确保摘要来源可重复。
        best = min(
            ordered_occurrences,
            key=lambda item: (
                -_resolution_rank(str(item.get("resolution") or "unresolved")),
                str(item.get("id") or ""),
            ),
        )
        attributes = dict(best.get("attributes") or {})
        attributes["occurrenceCount"] = len(occurrences)
        # 显式标明存在性语义，路径分析不得把聚合摘要当作连续调用。
        attributes["resolutionSemantics"] = "exists-witness"
        attributes["bestWitnessId"] = str(best.get("id") or "")
        attributes["bindingKindsObserved"] = bindings
        edge_id = stable_id(
            "topology-edge", source, target, relation, contract_key, operation, _file
        )
        finalized[edge_id] = {
            "id": edge_id,
            "from": source,
            "to": target,
            "relation": relation,
            "resolution": resolution,
            "existsResolution": resolution,
            "derivations": [
                derivation
                for item in ordered_occurrences
                for derivation in item.get("derivations", ())
            ],
            "bestWitnessId": str(best.get("id") or ""),
            # 保留全部见证引用，展示截断不会切断审计链路。
            "witnessIds": sorted(str(item.get("id") or "") for item in occurrences),
            "coverage": {
                "occurrences": len(occurrences),
                "byResolution": dict(
                    sorted(
                        Counter(
                            str(item.get("resolution") or "unresolved") for item in occurrences
                        ).items()
                    )
                ),
                "representativeSitesOmitted": max(
                    0,
                    len({_call_site_sort_key(item.get("callSite") or {}) for item in occurrences})
                    - len(call_sites),
                ),
                # 省略数量与截断后的展示分开报告，消费者能识别未展示证据。
                "evidenceOmitted": max(0, len(evidence) - _MAX_AGGREGATED_EVIDENCE),
            },
            "operation": operation,
            "contractKey": contract_key,
            "evidence": evidence[:_MAX_AGGREGATED_EVIDENCE],
            "attributes": attributes,
            "provenance": dict(best.get("provenance") or {}),
            **({"callSite": dict(best["callSite"])} if best.get("callSite") else {}),
            "representativeCallSites": call_sites,
        }
    return finalized


# 缺省绑定类型由解析等级导出，调用方额外属性统一在末尾合并。
def _semantic_edge_attributes(edge: SemanticEdge, **extra: Any) -> dict[str, Any]:
    binding = (
        edge.binding_kind
        or {
            "exact": "declared-exact",
            "candidate": "runtime-candidate",
            "heuristic": "heuristic",
            "unresolved": "unresolved",
        }[edge.resolution]
    )
    return {
        "bindingKind": binding,
        "analysisCompleteness": edge.analysis_completeness,
        "contextId": getattr(edge, "analysis_context_id", ""),
        **extra,
    }
