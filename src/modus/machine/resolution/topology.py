"""Compose service, component and resource topology from reconciled facts.

Identity/classification policy, occurrence serialization and call lifting have
separate owners. This entry point preserves their order, attaches derivations,
then validates the complete projection before returning it to publication.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from modus.config import ModusConfig
from modus.machine.errors import InvariantError
from modus.machine.facts.semantic_ir import SemanticEdge, SemanticFacts, SemanticNode, stable_id
from modus.machine.facts.semantic_providers import PROVIDER_VERSIONS, CompilerSemanticResult
from modus.machine.resolution.evidence_graph import iter_keyed_edges
from modus.machine.resolution.method_graph import build_method_graph
from modus.machine.resolution.semantic_reconcile import SemanticReconcileResult
from modus.machine.resolution.topology_model import (
    TOPOLOGY_SCHEMA_VERSION,
)
from modus.machine.resolution.topology_model import (
    derivation as _derivation,
)
from modus.machine.resolution.topology_records import (
    _finalize_occurrence_edges,
    _occurrence_edge,
    _semantic_edge_attributes,
    _topology_edge,
    _topology_node,
)
from modus.machine.resolution.topology_resources import (
    _lift_calls,
)
from modus.machine.resolution.topology_rules import (
    _COMPONENT_ANNOTATIONS,
    _ENDPOINT_ANNOTATIONS,
    _annotation_resolution,
    _annotations,
    _endpoint_contract,
    _has_annotation,
    _has_composed_annotation,
    _is_dubbo_service,
    _is_grpc_service,
    _resolve_owner,
    _select_deployable,
    _source_scope,
    _type_id_lookup,
)
from modus.machine.resolution.topology_validation import validate_topology
from modus.paths import ModusPaths

_COMPONENT_TYPE_NAME = re.compile(
    r"(?:Controller|Handler|Service|Facade|Repository|Store|Client|Consumer|Producer|Manager|Usecase|UseCase)$"
)


# 启动入口是部署候选的证据，普通工具 main 不自动变成独立服务。
def _deployable_candidates(facts: SemanticFacts) -> dict[str, list[dict[str, Any]]]:
    """Collect deployable-service nodes per build unit from bootstrap entrypoints.

    Only Spring Boot bootstrap annotations and Go main functions count as
    deployment boundaries; each candidate keeps its bootstrap symbol anchor.
    """
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in facts.nodes:
        if not node.source_anchors:
            continue
        annotation_items = _annotations(node)
        name = node.qualified_name
        strong = _has_annotation(annotation_items, ("SpringBootApplication",))
        strong = strong or (
            node.language == "go" and node.kind == "function" and ".mainfunc(" in name
        )
        # Utility/demo Java mains and C++ binaries do not prove independently
        # deployed services. Require the service bootstrap annotation here;
        # deployment manifests may contribute separate verified boundaries.
        if not strong:
            continue
        anchor = node.source_anchors[0]
        deployable_id = stable_id("deployable", node.id)
        result[node.build_unit_id].append(
            _topology_node(
                node_id=deployable_id,
                kind="deployable-service",
                qualified_name=node.qualified_name,
                build_unit_id=node.build_unit_id,
                language=node.language,
                source_anchors=(item.to_dict() for item in node.source_anchors),
                attributes={
                    "boundaryEvidence": ["compiler entrypoint or service bootstrap"],
                    "sourceScope": _source_scope(anchor.file),
                    "bootstrapSymbolId": node.id,
                },
                provenance=node.provenance.to_dict() if node.provenance else {},
            )
        )
    return result


# 在拓扑投影前检查协调摘要和保留数量，防止消费不完整协调状态。
def _require_complete_reconcile(reconcile: SemanticReconcileResult) -> None:
    """Raise ValueError unless the reconcile result retained every semantic edge."""
    if not reconcile.digest or reconcile.stats.get("retainedSemanticEdges") != len(
        reconcile.facts.edges
    ):
        raise InvariantError(
            "machine.resolution.invalid-topology",
            "topology requires a complete evidence reconciliation result",
        )


# 先建立仓库与构建单元容器，后续组件归属始终有明确父级。
def _add_repository_and_build_units(
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    paths: ModusPaths,
    config: ModusConfig,
    compiler: CompilerSemanticResult,
) -> str:
    """Insert the repository node plus one contained build-unit node per unit.

    Mutates `nodes`/`edges` in place and returns the resolved repository ID
    (configured identity or the repository directory name).
    """
    repository_id = config.init_analysis.topology.repository_id or paths.root.name
    repository_node = f"repository:{repository_id}"
    nodes[repository_node] = _topology_node(
        node_id=repository_node,
        kind="repository",
        qualified_name=repository_id,
        build_unit_id="repository",
        attributes={"configuredIdentity": bool(config.init_analysis.topology.repository_id)},
    )
    for unit in compiler.build_context.units:
        node_id = f"build-unit:{unit.id}"
        nodes[node_id] = _topology_node(
            node_id=node_id,
            kind="build-unit",
            qualified_name=unit.id,
            build_unit_id=unit.id,
            language=unit.language,
            attributes={"buildSystem": unit.build_system, "contextDigest": unit.context_digest},
        )
        edge = _topology_edge(
            source=repository_node,
            target=node_id,
            relation="contains",
            resolution="exact",
            evidence=("Build Context Resolver",),
        )
        edges[edge["id"]] = edge
    return repository_id


# 部署候选保留其构建单元边界，避免把不同构建产物合成服务。
def _add_deployables(
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    deployables: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Insert deployable-service nodes and their contains edges from build units."""
    for unit_id, unit_deployables in deployables.items():
        for deployable in unit_deployables:
            nodes[deployable["id"]] = deployable
            edge = _topology_edge(
                source=f"build-unit:{unit_id}",
                target=deployable["id"],
                relation="contains",
                resolution="exact",
                evidence=("entrypoint/deployment boundary",),
            )
            edges[edge["id"]] = edge


# 组件由语义类型和框架依据派生，无法唯一归属部署时保留构建单元归属。
def _collect_components(
    facts: SemanticFacts,
    type_lookup: Mapping[tuple[str, str, str], str],
    deployables: Mapping[str, list[dict[str, Any]]],
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Detect components and attach each under its deployable or build unit.

    Returns (semantic type ID -> component node ID, semantic type ID -> raw
    annotation tuple); `nodes`/`edges` gain one row per detected component.
    """
    component_ids: dict[str, str] = {}
    component_annotations: dict[str, tuple[str, ...]] = {}
    method_owners = {
        _resolve_owner(item, type_lookup)
        for item in facts.nodes
        if item.kind in {"method", "function"} and _resolve_owner(item, type_lookup)
    }
    for node in facts.nodes:
        if node.kind != "type" or not node.source_anchors:
            continue
        annotation_items = _annotations(node)
        annotated_component = _has_composed_annotation(annotation_items, _COMPONENT_ANNOTATIONS)
        typed_component_candidate = (
            node.language in {"go", "cpp"}
            and node.id in method_owners
            and _COMPONENT_TYPE_NAME.search(node.qualified_name) is not None
        )
        if not annotated_component and not typed_component_candidate:
            continue
        component_id = stable_id("component", node.id)
        component_ids[node.id] = component_id
        component_annotations[node.id] = annotation_items
        nodes[component_id] = _topology_node(
            node_id=component_id,
            kind="component",
            qualified_name=node.qualified_name,
            build_unit_id=node.build_unit_id,
            language=node.language,
            source_anchors=(item.to_dict() for item in node.source_anchors),
            attributes={
                "annotations": list(_annotations(node)),
                "sourceSymbolId": node.id,
                "boundaryEvidence": [
                    "framework component annotation"
                    if annotated_component
                    else "typed receiver plus component naming rule"
                ],
            },
            provenance=node.provenance.to_dict() if node.provenance else {},
        )
        owner = _select_deployable(
            deployables.get(node.build_unit_id, []),
            node.source_anchors[0].file,
        )
        parent = owner["id"] if owner else f"build-unit:{node.build_unit_id}"
        edge = _topology_edge(
            source=parent,
            target=component_id,
            relation="contains",
            resolution="candidate"
            if owner
            else _annotation_resolution(annotation_items)
            if annotated_component
            else "heuristic",
            evidence=(
                "framework component annotation"
                if annotated_component
                else "typed receiver plus component naming rule",
            ),
        )
        edges[edge["id"]] = edge
    return component_ids, component_annotations


# 接口映射保留事实次序，供 RPC 端点对齐声明契约。
def _implemented_interfaces(facts: SemanticFacts) -> dict[str, list[str]]:
    """Group implemented interface node IDs by implementing type, in fact order."""
    implements: dict[str, list[str]] = defaultdict(list)
    for semantic_edge in facts.edges:
        if semantic_edge.relation == "implements":
            implements[semantic_edge.source].append(semantic_edge.target)
    return implements


# 注入关系单独保留发生证据，不能用一般调用边代替依赖注入语义。
def _lift_injections(
    facts: SemanticFacts,
    component_ids: Mapping[str, str],
    raw_occurrence_edges: list[dict[str, Any]],
) -> dict[str, SemanticEdge]:
    """Emit injects occurrences between known components.

    Appends to `raw_occurrence_edges` in fact order and returns every injects
    edge keyed by semantic edge ID for later receiver-binding lookups.
    """
    injection_edges = {item.id: item for item in facts.edges if item.relation == "injects"}
    for semantic_edge in injection_edges.values():
        source = component_ids.get(semantic_edge.source)
        target = component_ids.get(semantic_edge.target)
        if source and target:
            raw_occurrence_edges.append(
                _occurrence_edge(
                    source=source,
                    target=target,
                    relation="injects",
                    resolution=semantic_edge.resolution,
                    operation=semantic_edge.operation,
                    evidence=semantic_edge.evidence,
                    call_site=semantic_edge.call_site.to_dict()
                    if semantic_edge.call_site
                    else None,
                    provenance=semantic_edge.provenance.to_dict()
                    if semantic_edge.provenance
                    else {},
                    attributes=_semantic_edge_attributes(semantic_edge),
                )
            )
    return injection_edges


# 入口契约与实现绑定分开保存，便于按契约检索并回查方法。
def _lift_endpoints(
    facts: SemanticFacts,
    semantic_nodes: Mapping[str, SemanticNode],
    type_lookup: Mapping[tuple[str, str, str], str],
    component_ids: Mapping[str, str],
    component_annotations: Mapping[str, tuple[str, ...]],
    implements: Mapping[str, list[str]],
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    entry_bindings: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> None:
    """Create endpoint nodes, exposes edges and entry bindings for component methods.

    Only methods owned by a known component qualify; candidates without source
    anchors are skipped with a warning appended to `diagnostics`.
    """
    for node in facts.nodes:
        if node.kind not in {"method", "function"}:
            continue
        annotation_items = _annotations(node)
        annotations = " ".join(annotation_items)
        owner_semantic = _resolve_owner(node, type_lookup)
        owner_annotation_items = component_annotations.get(owner_semantic, ())
        owner_annotations = " ".join(owner_annotation_items)
        owner_is_dubbo = _is_dubbo_service(owner_annotation_items)
        owner_is_grpc = _is_grpc_service(owner_annotation_items)
        method_is_endpoint = _has_composed_annotation(annotation_items, _ENDPOINT_ANNOTATIONS)
        if not method_is_endpoint and not owner_is_dubbo and not owner_is_grpc:
            continue
        endpoint_owner = component_ids.get(owner_semantic)
        if endpoint_owner is None:
            continue
        if not node.source_anchors:
            diagnostics.append(
                {
                    "recordKind": "diagnostic",
                    "code": "machine.resolution.endpoint-missing-source-anchor",
                    "message": "Skipped an endpoint candidate without repository source evidence.",
                    "severity": "warning",
                    "provider": "topology",
                    "buildUnitId": node.build_unit_id,
                    "subject": node.id,
                }
            )
            continue
        rpc_qualified_name = ""
        if owner_is_dubbo:
            interfaces = sorted(implements.get(owner_semantic, ()))
            if len(interfaces) == 1:
                descriptor = node.qualified_name.partition("#")[2]
                interface = semantic_nodes.get(interfaces[0])
                rpc_qualified_name = (
                    (
                        interface.qualified_name
                        if interface
                        else interfaces[0].removeprefix(f"{node.language}:")
                    )
                    + "#"
                    + descriptor
                )
        contract_key = _endpoint_contract(
            node,
            annotations,
            owner_annotations,
            rpc_qualified_name=rpc_qualified_name,
            owner_is_dubbo=owner_is_dubbo,
            owner_is_grpc=owner_is_grpc,
            method_is_grpc=_is_grpc_service(annotation_items),
        )
        endpoint_id = stable_id("endpoint", node.id, annotations)
        nodes[endpoint_id] = _topology_node(
            node_id=endpoint_id,
            kind="endpoint",
            qualified_name=node.qualified_name,
            build_unit_id=node.build_unit_id,
            language=node.language,
            source_anchors=(item.to_dict() for item in node.source_anchors),
            attributes={
                "annotations": list(_annotations(node)),
                "implementation": node.id,
                "contractKey": contract_key,
            },
            provenance=node.provenance.to_dict() if node.provenance else {},
        )
        entry_bindings.append(
            {
                "entryId": endpoint_id,
                "symbolId": node.id,
                "contextId": getattr(node, "analysis_context_id", ""),
                "resolution": _annotation_resolution((*annotation_items, *owner_annotation_items)),
            }
        )
        lifted = _topology_edge(
            source=endpoint_owner,
            target=endpoint_id,
            relation="exposes",
            resolution=_annotation_resolution((*annotation_items, *owner_annotation_items)),
            contract_key=contract_key,
            evidence=("framework endpoint annotation",),
            call_site=node.source_anchors[0].to_dict(),
            provenance=node.provenance.to_dict() if node.provenance else {},
        )
        edges[lifted["id"]] = lifted


# 派生记录关联原图边与源码，聚合后仍可追踪事实来源。
# 缺少原图边时保留空派生集合，不能凭端点相同伪造证据关联。
def _attach_occurrence_derivations(
    raw_occurrence_edges: list[dict[str, Any]],
    reconcile: SemanticReconcileResult,
) -> None:
    """Set each occurrence's derivations from its base graph edge plus source file.

    Occurrences without a matching base edge or call-site file keep an empty
    derivation list; records are mutated in place.
    """
    base_edges = {
        str(data.get("semantic_edge_id") or ""): key
        for _source, _target, key, data in iter_keyed_edges(reconcile.graph)
        if data.get("semantic_edge_id")
    }
    for occurrence in raw_occurrence_edges:
        attributes = occurrence.get("attributes") or {}
        base_id = base_edges.get(str(attributes.get("functionEdgeId") or ""))
        premises = ["edge:" + base_id] if base_id else []
        site = occurrence.get("callSite") or {}
        if site.get("file"):
            premises.append("source:" + str(site["file"]))
        occurrence["derivations"] = (
            [
                _derivation(
                    "machine.resolution." + str(occurrence["relation"]),
                    premises,
                    str(attributes.get("contextId") or "syntax"),
                )
            ]
            if premises
            else []
        )


# 按容器、组件、入口和调用顺序构造拓扑，依赖身份先于关系建立。
# 逐次发生记录与聚合边同时交付，方法链按需生成。
# 发布前执行完整拓扑验证，使下游可以依赖统一的不变量。
def build_topology(
    paths: ModusPaths,
    config: ModusConfig,
    compiler: CompilerSemanticResult,
    *,
    reconcile: SemanticReconcileResult,
) -> dict[str, Any]:
    _require_complete_reconcile(reconcile)
    facts = reconcile.facts
    semantic_nodes = {item.id: item for item in facts.nodes}
    type_lookup = _type_id_lookup(facts)

    entry_bindings: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    diagnostics = [item.to_dict() for item in facts.diagnostics]
    repository_id = _add_repository_and_build_units(nodes, edges, paths, config, compiler)
    deployables = _deployable_candidates(facts)
    _add_deployables(nodes, edges, deployables)
    component_ids, component_annotations = _collect_components(
        facts, type_lookup, deployables, nodes, edges
    )
    implements = _implemented_interfaces(facts)

    raw_occurrence_edges: list[dict[str, Any]] = []
    injection_edges = _lift_injections(facts, component_ids, raw_occurrence_edges)
    _lift_endpoints(
        facts,
        semantic_nodes,
        type_lookup,
        component_ids,
        component_annotations,
        implements,
        nodes,
        edges,
        entry_bindings,
        diagnostics,
    )
    _lift_calls(
        paths,
        facts,
        semantic_nodes,
        type_lookup,
        component_ids,
        deployables,
        injection_edges,
        nodes,
        raw_occurrence_edges,
    )

    _attach_occurrence_derivations(raw_occurrence_edges, reconcile)
    edges.update(_finalize_occurrence_edges(raw_occurrence_edges, diagnostics))
    raw_occurrences = len(raw_occurrence_edges)
    aggregated_edges = sum(
        1 for item in edges.values() if "occurrenceCount" in (item.get("attributes") or {})
    )

    method_graph = build_method_graph(reconcile, nodes, raw_occurrence_edges, entry_bindings)
    result = {
        "schemaVersion": TOPOLOGY_SCHEMA_VERSION,
        "repositoryId": repository_id,
        "source": {
            "providerVersions": dict(PROVIDER_VERSIONS),
            "mode": compiler.mode,
            "semanticReconcileDigest": reconcile.digest,
        },
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(edges.values(), key=lambda item: item["id"]),
        "chains": [],
        "chainMode": "on-demand",
        "methodGraph": method_graph,
        "occurrences": sorted(raw_occurrence_edges, key=lambda item: item["id"]),
        "diagnostics": diagnostics,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "deployables": sum(len(items) for items in deployables.values()),
            "components": len(component_ids),
            "exactEdges": sum(1 for item in edges.values() if item["resolution"] == "exact"),
            "unresolvedEdges": sum(
                1 for item in edges.values() if item["resolution"] == "unresolved"
            ),
            "rawOccurrences": raw_occurrences,
            "aggregatedEdges": aggregated_edges,
            "chains": 0,
            "methodNodes": len(method_graph["nodes"]),
            "methodEdges": len(method_graph["edges"]),
        },
    }
    validate_topology(result)
    return result


__all__ = [
    "TOPOLOGY_SCHEMA_VERSION",
    "build_topology",
    "build_method_graph",
    "validate_topology",
]
