"""Project resolved semantic witnesses into a bounded method graph.

This module owns symbol-level graph assembly. It consumes only the reconciled
N3 model and topology identities; it never reads source files or persists data.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from modus.machine.facts.semantic_ir import stable_id
from modus.machine.resolution.evidence_graph import iter_keyed_edges
from modus.machine.resolution.semantic_reconcile import SemanticReconcileResult
from modus.machine.resolution.topology_model import (
    derivation,
    resolution_rank,
    semantic_owner,
)

# 链路视图只接收显式允许的行为关系，结构包含边不作为执行步骤。
_CHAIN_RELATIONS = frozenset(
    {
        "calls",
        "invokes",
        "reads",
        "writes",
        "invalidates",
        "publishes",
        "consumes",
        "transitions_to",
        "configured_by",
        "uses",
        "enters",
        "handled_by",
    }
)


# 旧图位置转换为最小源码锚点，缺少路径或行号时不伪造位置。
def _graph_anchor(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    path = str(data.get("source_file") or "")
    location = re.match(r"L?(\d+)(?::(\d+))?", str(data.get("source_location") or ""))
    if not path or location is None:
        return []
    return [
        {"file": path, "line": max(1, int(location[1])), "column": max(1, int(location[2] or 1))}
    ]


# 先保留符号级见证，再供架构视图聚合，避免提前丢失调用位置。
# 只消费协调后的图和拓扑身份，不重新读取源码。
# 跨分析上下文的边排除并计数，防止混合不同构建配置。
def build_method_graph(
    reconcile: SemanticReconcileResult,
    topology_nodes: Mapping[str, Mapping[str, Any]],
    occurrences: Sequence[Mapping[str, Any]],
    entry_bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Index symbol-level witnesses before architectural aggregation."""

    graph = reconcile.graph
    bindings = {item.semantic_id: item.graph_node_id for item in reconcile.bindings}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    entries: dict[str, dict[str, Any]] = {}
    # 覆盖计数记录被边界约束排除的边，空结果不等于不存在依赖。
    dropped_context_edges = 0
    owners: dict[str, list[str]] = defaultdict(list)
    for parent, child, data in graph.edges(data=True):
        if data.get("relation") == "contains":
            owners[str(child)].append(str(parent))
    # 类型身份包含语言和分析上下文，避免跨构建同名类型串联。
    semantic_types = {
        (item.language, item.qualified_name, item.analysis_context_id): item.id
        for item in reconcile.facts.nodes
        if item.kind == "type"
    }
    # 架构组件通过原始符号 ID 对齐，不依赖展示名称。
    components = {
        str((row.get("attributes") or {}).get("sourceSymbolId")): row
        for row in topology_nodes.values()
        if row.get("kind") == "component"
    }
    semantic_components: dict[str, Mapping[str, Any]] = {}
    for item in reconcile.facts.nodes:
        owner = semantic_owner(item)
        owner = semantic_types.get(
            (item.language, owner.removeprefix(item.language + ":"), item.analysis_context_id),
            owner,
        )
        if owner in components and item.id in bindings:
            semantic_components[bindings[item.id]] = components[owner]

    # 优先使用编译器所有者映射，再回退到结构包含关系。
    # 只有唯一候选所有者才建立组件归属，歧义保持为空。
    def component_of(identity: str) -> dict[str, str]:
        component = semantic_components.get(identity)
        if component:
            return {
                "componentId": str(component["id"]),
                "kind": "component",
                "qualifiedName": str(component["qualifiedName"]),
            }
        candidates = [
            owner
            for owner in owners.get(identity, ())
            if graph.nodes[owner].get("kind") in {"class", "type", "module", "file", "component"}
        ]
        # 类或类型的直接所有权优先于文件容器，保持方法归属的精度。
        concrete = [
            owner
            for owner in candidates
            if graph.nodes[owner].get("kind") not in {"module", "file"}
        ]
        selected = concrete or candidates
        if len(set(selected)) != 1:
            return {}
        owner = selected[0]
        data = graph.nodes[owner]
        return {
            "componentId": owner,
            "kind": str(data.get("kind") or "component"),
            "qualifiedName": str(data.get("qualified_name") or data.get("label") or owner),
        }

    # 节点首次观察时归一化公共属性，后续边共享同一记录。
    # 入口种类另建索引，使遍历无需扫描全部普通符号。
    def observe_node(node_id: str) -> str:
        identity = node_id
        if identity in nodes:
            return identity
        data = graph.nodes[node_id]
        context = str(data.get("analysis_context_id") or data.get("context_id") or "syntax")
        nodes[identity] = {
            "id": identity,
            "symbolId": identity,
            "kind": str(data.get("kind") or "symbol"),
            "qualifiedName": str(data.get("qualified_name") or data.get("label") or identity),
            "contextId": context,
            "sourceAnchors": _graph_anchor(data),
            "external": bool(data.get("external")),
            "analysisCompleteness": str(data.get("analysis_completeness") or "partial"),
            "component": component_of(identity),
            "contracts": list(data.get("contracts") or ()),
        }
        if data.get("kind") in {"route", "page-route", "page-action", "endpoint", "entrypoint"}:
            entries[identity] = {"entryId": identity, "symbolId": identity, "contextId": context}
        return identity

    for node_id, data in graph.nodes(data=True):
        if data.get("kind") in {
            "function",
            "method",
            "route",
            "page-route",
            "page-action",
            "endpoint",
            "entrypoint",
        }:
            observe_node(node_id)
    for source, target, key, data in iter_keyed_edges(graph):
        relation = str(data.get("relation") or "")
        # 过滤发生在节点扩展前，避免无关关系把整个图带入方法视图。
        if relation not in _CHAIN_RELATIONS:
            continue
        source_id, target_id = observe_node(source), observe_node(target)
        context = str(
            data.get("analysis_context_id")
            or data.get("context_id")
            or nodes[source_id]["contextId"]
        )
        # 关系及两端必须共享上下文，遗漏的连接通过覆盖统计暴露。
        if nodes[source_id]["contextId"] != context or nodes[target_id]["contextId"] != context:
            dropped_context_edges += 1
            continue
        resolution = str(
            data.get("declaration_resolution") or data.get("resolution") or "unresolved"
        )
        dispatch = str(data.get("dispatch_resolution") or "not-analyzed")
        binding = str(data.get("binding_kind") or data.get("bindingKind") or "unresolved")
        # 运行时分派只有候选时降低整体解析等级，即使声明本身精确。
        if dispatch == "candidates" or binding == "runtime-candidate":
            resolution = min((resolution, "candidate"), key=resolution_rank)
        edges[key] = {
            "id": key,
            "from": source_id,
            "to": target_id,
            "relation": relation,
            "resolution": resolution,
            "bindingKind": binding,
            "dispatchResolution": dispatch,
            "contextId": context,
            "derivations": [
                derivation("machine.projection.method-relation", ["edge:" + key], context)
            ],
            # 见证 ID 保留原图边键，消费者可追溯到具体调用观察。
            "witnessIds": [key],
            "callSite": data.get("call_site")
            or data.get("callSite")
            or next(iter(_graph_anchor(data)), None),
            "provenance": data.get("provenance")
            or data.get("semantic_provenance")
            or {"provider": "structure"},
        }
    # 资源副作用通过语义边 ID 回查，保留从派生结果到原始事实的路径。
    semantic_edges = {item.id: item for item in reconcile.facts.edges}
    for occurrence in occurrences:
        # 调用与注入已有专门表示，资源补充阶段不重复生成。
        if occurrence["relation"] in {"calls", "injects"}:
            continue
        attrs = occurrence.get("attributes") or {}
        fact = semantic_edges.get(str(attrs.get("functionEdgeId") or ""))
        if fact is None:
            continue
        source_id = bindings.get(fact.source, fact.source)
        if source_id not in graph:
            continue
        observe_node(source_id)
        target_id = str(occurrence["to"])
        context = nodes[source_id]["contextId"]
        resource = topology_nodes[target_id]
        # 资源节点继承当前方法上下文，以便后续链路查询检查边界。
        nodes[target_id] = {**dict(resource), "contextId": context, "external": False}
        identity = str(occurrence["id"])
        edges[identity] = {
            "id": identity,
            "from": source_id,
            "to": target_id,
            "relation": occurrence["relation"],
            "resolution": occurrence["resolution"],
            "bindingKind": attrs.get("bindingKind", "unresolved"),
            "dispatchResolution": fact.dispatch_precision,
            "contextId": context,
            "witnessIds": [identity],
            "sourceFactId": fact.id,
            "callSite": occurrence.get("callSite"),
            "provenance": occurrence.get("provenance", {}),
            # 资源分类未知时明确标记，避免缺省值表现为已确认分类。
            "classificationEvidence": attrs.get("classificationEvidence", "unclassified"),
            "effect": attrs.get("effect", occurrence["relation"]),
            "operation": occurrence.get("operation", ""),
            "valueSummary": attrs.get("valueSummary", {}),
            "derivations": list(occurrence.get("derivations") or ()),
        }
    # 架构入口通过显式 enters 边连接实现，入口身份与方法身份分别保留。
    for entry_binding in entry_bindings:
        entry_id = str(entry_binding["entryId"])
        # 优先连接协调后的语义观察节点，兼容原符号 ID 作为回退。
        target_id = bindings.get(str(entry_binding["symbolId"]), str(entry_binding["symbolId"]))
        if target_id not in graph:
            continue
        observe_node(target_id)
        context = nodes[target_id]["contextId"]
        nodes[entry_id] = {
            **dict(topology_nodes[entry_id]),
            "contextId": context,
            "symbolId": target_id,
            "component": nodes[target_id].get("component", {}),
        }
        entries[entry_id] = {"entryId": entry_id, "symbolId": target_id, "contextId": context}
        # 入口连接由端点身份稳定派生，重复构建保持可比较。
        identity = stable_id("entry-binding", entry_id, target_id)
        edges[identity] = {
            "id": identity,
            "from": entry_id,
            "to": target_id,
            "relation": "enters",
            "resolution": str(entry_binding.get("resolution") or "candidate"),
            "contextId": context,
            "bindingKind": "declared-exact",
            "witnessIds": [identity],
            "provenance": topology_nodes[entry_id].get("provenance", {}),
            "derivations": [
                derivation(
                    "machine.resolution.entry-binding",
                    ["node:" + target_id]
                    + [
                        "source:" + str(anchor["file"])
                        for anchor in nodes[target_id].get("sourceAnchors", ())
                    ],
                    context,
                )
            ],
        }
    # 输出按身份排序，使相同事实得到稳定序列化结果。
    return {
        "schemaVersion": 1,
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(edges.values(), key=lambda item: item["id"]),
        "entries": sorted(entries.values(), key=lambda item: item["entryId"]),
        "coverage": {"contextEdgesExcluded": dropped_context_edges},
    }


__all__ = ["build_method_graph"]
