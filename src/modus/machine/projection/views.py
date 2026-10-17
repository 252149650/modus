"""Purpose-built JSON projections for initial knowledge authoring.

The reconciled graph is intentionally consumed here and never serialized.
Every persisted relationship remains traceable to frozen source coordinates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from modus.machine.errors import validate_code
from modus.machine.store.contracts import SCHEMA_VERSION

_RESOLUTIONS = frozenset({"exact", "candidate", "heuristic", "unresolved"})
_SERVICE_NODE_KINDS = frozenset(
    {
        # Native topology kinds emitted by ``build_topology``.
        # A plain component is retained only when it participates in a service edge.
        "deployable-service",
        "endpoint",
        "rpc-service",
        "topic",
        "db",
        "keyspace",
        "config-key",
        # Compatibility with externally supplied and legacy topology documents.
        "service",
        "api",
        "job",
        "message",
        "database",
        "external-system",
        "resource",
    }
)
_SERVICE_EDGE_KINDS = frozenset(
    {
        # Native topology relations emitted by ``build_topology``.
        "exposes",
        "calls",
        "invokes",
        "publishes",
        "consumes",
        "reads",
        "writes",
        "invalidates",
        "configured_by",
        # Compatibility with externally supplied and legacy topology documents.
        "schedules",
        "configures",
    }
)


# 兼容不同生产者的来源字段，但统一输出固定层级。
# 优先识别调和结果，避免将其降成单个编译器观察。
def _source_layer(row: Mapping[str, Any]) -> str:
    origin = " ".join(
        str(row.get(key) or "")
        for key in ("source_layer", "sourceLayer", "resolvedBy", "binding_kind", "_origin")
    ).lower()
    if "reconcil" in origin:
        return "reconciled"
    if "compiler" in origin or "semantic" in origin:
        return "compiler"
    if "bind" in origin or "import" in origin:
        return "project-binding"
    return "cst"


# 调用点优先于通用锚点，因为它更直接支持当前关系。
# 旧拓扑格式仍可提供代表位置，但缺失时返回空而不造位置。
def _service_edge_witness(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the best source anchor from native or legacy topology edges."""

    call_site = row.get("callSite") or row.get("call_site")
    if isinstance(call_site, Mapping) and call_site:
        return call_site
    for key in ("sourceAnchors", "anchors", "representativeCallSites"):
        anchors = row.get(key)
        if isinstance(anchors, (list, tuple)) and anchors and isinstance(anchors[0], Mapping):
            return anchors[0]
    return {}


# 未知精度依据候选或目标信息保守回退。
# 存在目标名称最多支持 heuristic，不足以证明 exact。
def _resolution(row: Mapping[str, Any]) -> str:
    value = str(row.get("resolution") or "")
    if value in _RESOLUTIONS:
        return value
    if row.get("candidateCount") or row.get("candidates"):
        return "candidate"
    if row.get("target") or row.get("targetReference"):
        return "heuristic"
    return "unresolved"


# 只保留创作需要的服务与资源关系，普通组件需参与相关边。
# 关系没有源码见证时转为覆盖缺口，不发布不可追溯连接。
# 来源层、替代候选和原因随关系保留，避免摘要隐去不确定性。
# 排序后的节点与边可稳定序列化，输入枚举顺序不影响视图。
def project_services(
    topology: Mapping[str, Any],
    *,
    baseline_digest: str,
    source_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Keep only initialization-relevant local service and resource relationships."""

    edges = []
    participants: set[str] = set()
    coverage_gaps = [
        item if isinstance(item, dict) else {"reasonCode": "machine.projection.topology-diagnostic"}
        for item in topology.get("diagnostics", [])
    ]
    for raw in topology.get("edges", []):
        if not isinstance(raw, Mapping):
            continue
        relation = str(raw.get("relation") or raw.get("kind") or "")
        if relation not in _SERVICE_EDGE_KINDS:
            continue
        row = dict(raw)
        row["relation"] = relation
        row["resolution"] = _resolution(row)
        row["sourceLayer"] = _source_layer(row)
        witness = _service_edge_witness(row)
        if not witness:
            coverage_gaps.append(
                {
                    "reasonCode": "machine.projection.service-relation-without-witness",
                    "relationId": str(row.get("id") or ""),
                }
            )
            continue
        witness_path = str(witness.get("path") or witness.get("file") or "")
        row["witness"] = {
            "path": witness_path,
            "symbol": str(row.get("symbol") or ""),
            "startLine": witness.get("startLine", witness.get("line")),
            "endLine": witness.get("endLine", witness.get("startLine", witness.get("line"))),
            "sourceDigest": str(
                witness.get("sourceDigest") or source_digests.get(witness_path) or ""
            ),
        }
        row["alternatives"] = list(row.get("candidates") or [])
        row["reason"] = str(row.get("reasonCode") or row.get("reason") or "")
        row["contextId"] = str(row.get("analysisContextId") or row.get("context_id") or "")
        edges.append(row)
        participants.update(
            str(value)
            for value in (row.get("source") or row.get("from"), row.get("target") or row.get("to"))
            if value
        )
    nodes = []
    # 节点过滤保留参与已选关系的组件，避免关系失去阅读上下文。
    # 节点种类兼容旧拼写后再判断。
    retained_ids: set[str] = set()
    for raw in topology.get("nodes", []):
        if not isinstance(raw, Mapping):
            continue
        identity = str(raw.get("id") or raw.get("name") or "")
        kind = str(raw.get("kind") or raw.get("type") or "").replace("_", "-")
        if not identity or (kind not in _SERVICE_NODE_KINDS and identity not in participants):
            continue
        retained_ids.add(identity)
        nodes.append({**dict(raw), "kind": kind})
    edges = [
        row
        for row in edges
        if str(row.get("source") or row.get("from") or "") in retained_ids
        or str(row.get("target") or row.get("to") or "") in retained_ids
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "baselineDigest": baseline_digest,
        "nodes": sorted(nodes, key=lambda row: str(row.get("id") or row.get("name") or "")),
        "edges": sorted(
            edges,
            key=lambda row: (
                str(row.get("source") or row.get("from") or ""),
                str(row.get("target") or row.get("to") or ""),
                str(row.get("relation") or ""),
            ),
        ),
        "coverageGaps": coverage_gaps,
    }


# 按阶段、语言、原因与精度聚合，保留真实数量和有限样本。
# 原因码先验证，任意异常文本不能直接变成公共协议标识。
# 每组样本最多三条，避免大仓库重复失败撑大诊断包。
# 阶段摘要只保留状态、整数统计和有限原因码。
# 解析、编译、调和、引用和投影缺口分别归组，便于定位责任边界。
def project_diagnostics(
    *,
    baseline_digest: str,
    stages: Mapping[str, Any],
    extraction: Mapping[str, Any],
    compiler: Mapping[str, Any],
    reconcile: Mapping[str, Any],
    unresolved: Mapping[str, Any],
    evidence_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def add(
        stage: str,
        row: object,
        *,
        fallback_reason: str,
        fallback_resolution: str = "unresolved",
    ) -> None:
        item = row if isinstance(row, Mapping) else {"detail": str(row)}
        reason = str(
            item.get("reasonCode") or item.get("code") or item.get("reason") or fallback_reason
        )
        try:
            validate_code(reason)
        except ValueError:
            reason = fallback_reason
        language = str(item.get("language") or "unknown")
        resolution = str(item.get("resolution") or fallback_resolution)
        if resolution not in _RESOLUTIONS:
            resolution = fallback_resolution
        key = (stage, language, reason, resolution)
        bucket = grouped.setdefault(
            key,
            {
                "stage": stage,
                "language": language,
                "reasonCode": reason,
                "resolution": resolution,
                "count": 0,
                "samples": [],
            },
        )
        bucket["count"] += 1
        raw_location = item.get("location")
        location: Mapping[str, Any] = raw_location if isinstance(raw_location, Mapping) else {}
        path = str(
            item.get("path")
            or item.get("callerFile")
            or location.get("path")
            or item.get("sourceFile")
            or ""
        )
        if path and len(bucket["samples"]) < 3:
            sample = {
                "path": path,
                "symbol": str(item.get("symbol") or item.get("name") or ""),
                "startLine": item.get("line") or location.get("startLine"),
            }
            if sample not in bucket["samples"]:
                bucket["samples"].append(sample)

    for row in extraction.get("diagnostics") or []:
        add("parser", row, fallback_reason="machine.facts.parser-diagnostic")
    build_context = compiler.get("buildContext")
    if isinstance(build_context, Mapping):
        for row in build_context.get("diagnostics") or []:
            add("compiler", row, fallback_reason="machine.facts.compiler-diagnostic")
    for row in compiler.get("providers") or []:
        if isinstance(row, Mapping) and row.get("analysisCompleteness") != "complete":
            add("compiler", row, fallback_reason="machine.facts.compiler-provider")
    for row in reconcile.get("conflicts") or []:
        add(
            "reconcile",
            row,
            fallback_reason="machine.resolution.conflict",
            fallback_resolution="candidate",
        )
    for row in unresolved.get("refs") or []:
        add("reference", row, fallback_reason="machine.resolution.reference-unresolved")
    for row in evidence_gaps:
        add("projection", row, fallback_reason="machine.projection.coverage-gap")

    # 压缩阶段信息时排除任意嵌套对象，防止内部图或大载荷泄露。
    # 布尔值不作为计数，避免状态字段被误纳入数值统计。
    compact_stages: dict[str, dict[str, Any]] = {}
    for name, raw in sorted(stages.items()):
        if not isinstance(raw, Mapping):
            compact_stages[name] = {"status": str(raw)}
            continue
        compact_stages[name] = {
            key: value
            for key, value in raw.items()
            if key == "status"
            or isinstance(value, int)
            and not isinstance(value, bool)
            or key == "reasonCode"
            and isinstance(value, str)
            and len(value) <= 200
        }
    groups = sorted(
        grouped.values(),
        key=lambda row: (
            str(row["stage"]),
            str(row["language"]),
            str(row["reasonCode"]),
            str(row["resolution"]),
        ),
    )
    # 提供者覆盖按完整度汇总，执行过不等于完整分析。
    # 原始覆盖统计仍保留在独立字段，方便解释统计口径。
    provider_counts: dict[str, int] = {}
    for row in compiler.get("providers") or []:
        if isinstance(row, Mapping):
            status = str(row.get("analysisCompleteness") or "unknown")
            provider_counts[status] = provider_counts.get(status, 0) + 1
    return {
        "schemaVersion": SCHEMA_VERSION,
        "baselineDigest": baseline_digest,
        "status": "partial" if groups else "complete",
        "stages": compact_stages,
        "groups": groups,
        "groupCount": len(groups),
        "totalCount": sum(int(row["count"]) for row in groups),
        "coverage": {
            "parserBinding": dict(extraction.get("bindingStats") or {}),
            "compilerProviders": dict(sorted(provider_counts.items())),
            "compiler": dict(compiler.get("coverage") or {}),
            "references": dict(unresolved.get("coverage") or {}),
        },
    }


__all__ = ["project_diagnostics", "project_services"]
