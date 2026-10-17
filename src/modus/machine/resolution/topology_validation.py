"""Validate topology identities, witness continuity, and context isolation.

Validation is intentionally separate from assembly so callers can authenticate
materialized graphs without activating providers or touching repository input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from modus.machine.errors import InvariantError
from modus.machine.resolution.topology_model import TOPOLOGY_SCHEMA_VERSION, resolution_rank


# 只合并相邻同组件步骤，保留离开后再次进入组件的路径顺序。
# 每个组件步骤保留原方法步骤 ID，展示压缩后仍能回查连续见证。
def _component_path(
    nodes: Mapping[str, Mapping[str, Any]], path: Sequence[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for identity in path:
        row = nodes[identity]
        component = row.get("component") or {}
        component_id = str(component.get("componentId") or identity)
        if result and result[-1]["componentId"] == component_id:
            result[-1]["stepIds"].append(identity)
            continue
        result.append(
            {
                "componentId": component_id,
                "kind": str(component.get("kind") or row.get("kind") or "symbol"),
                "qualifiedName": str(
                    component.get("qualifiedName") or row.get("qualifiedName") or identity
                ),
                "stepIds": [identity],
            }
        )
    return result


# 先检查身份和端点，再检查解析等级及发生数量，拒绝悬空关系。
def _validate_edges(edges: list[Any], ids: set[Any]) -> None:
    """Require unique edge identities, admitted endpoints and valid evidence counts."""
    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, Mapping) or not isinstance(edge.get("id"), str):
            raise InvariantError(
                "machine.resolution.invalid-topology", "topology edge must be an object with an ID"
            )
        if edge["id"] in edge_ids:
            raise InvariantError(
                "machine.resolution.invalid-topology", "topology edges must have unique IDs"
            )
        edge_ids.add(edge["id"])
        if edge.get("from") not in ids or edge.get("to") not in ids:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                f"topology edge {edge['id']} has a missing endpoint",
            )
        if edge.get("resolution") not in {"exact", "candidate", "heuristic", "unresolved"}:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                f"topology edge {edge['id']} has invalid resolution",
            )
        occurrence_count = (edge.get("attributes") or {}).get("occurrenceCount")
        if occurrence_count is not None and (
            type(occurrence_count) is not int or occurrence_count < 1
        ):
            raise InvariantError(
                "machine.resolution.invalid-topology",
                f"topology edge {edge['id']} has invalid occurrenceCount",
            )


# 方法图先复用结构验证，再追加上下文一致性约束。
# 入口绑定必须同时指向存在的入口与符号。
def _validate_method_graph(method: Any) -> None:
    """Validate nested graph structure before enforcing context isolation."""
    if method is not None:
        if not isinstance(method, Mapping) or method.get("schemaVersion") != 1:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                "topology methodGraph must use schemaVersion 1",
            )
        validate_topology(
            {
                "schemaVersion": TOPOLOGY_SCHEMA_VERSION,
                "nodes": method.get("nodes"),
                "edges": method.get("edges"),
            }
        )
        method_nodes = {row["id"]: row for row in method["nodes"]}
        for edge in method["edges"]:
            source_context = method_nodes[edge["from"]].get("contextId", "syntax")
            target_context = method_nodes[edge["to"]].get("contextId", "syntax")
            if (
                source_context != target_context
                or edge.get("contextId", "syntax") != source_context
            ):
                raise InvariantError(
                    "machine.resolution.invalid-topology",
                    "topology method edge crosses analysis contexts",
                )
        for entry in method.get("entries", ()):
            if (
                entry.get("entryId") not in method_nodes
                or entry.get("symbolId") not in method_nodes
            ):
                raise InvariantError(
                    "machine.resolution.invalid-topology",
                    "topology entry binding has a missing symbol",
                )


# 聚合等级必须属于真实最佳见证，不能只信摘要声明。
# 见证列表中的每个 ID 都要能回查发生记录。
def _validate_occurrences(value: Mapping[str, Any], edges: list[Any]) -> None:
    """Tie aggregate certainty to an existing occurrence witness."""
    occurrences = value.get("occurrences", ())
    occurrence_rows = {row["id"]: row for row in occurrences}
    if len(occurrence_rows) != len(occurrences):
        raise InvariantError(
            "machine.resolution.invalid-topology", "topology occurrences must have unique IDs"
        )
    for edge in edges:
        if "existsResolution" not in edge:
            continue
        if any(identity not in occurrence_rows for identity in edge["witnessIds"]):
            raise InvariantError(
                "machine.resolution.invalid-topology",
                "topology aggregate has a missing occurrence witness",
            )
        best = occurrence_rows.get(edge.get("bestWitnessId"))
        if best is None or best["resolution"] != edge["resolution"]:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                "topology aggregate resolution must belong to its best witness",
            )


# 链路必须逐步连续且方向一致，节点集合相同不足以证明路径有效。
# 整体精度由最弱边决定，组件视图必须保持方法路径的顺序。
def _validate_chains(
    value: Mapping[str, Any], nodes: list[Any], edges: list[Any], ids: set[Any], method: Any
) -> None:
    """Check ordered path continuity and derive certainty from its weakest edge."""
    chains = value.get("chains")
    if chains is None:
        return
    if not isinstance(chains, list):
        raise InvariantError(
            "machine.resolution.invalid-topology", "topology chains must be a list"
        )
    chain_ids: set[str] = set()
    chain_nodes = ids | ({row["id"] for row in method["nodes"]} if method else set())
    chain_edges = {row["id"]: row for row in (method["edges"] if method else edges)}
    for chain in chains:
        if not isinstance(chain, Mapping) or not isinstance(chain.get("chainId"), str):
            raise InvariantError(
                "machine.resolution.invalid-topology",
                "topology chain must be an object with a chainId",
            )
        if chain["chainId"] in chain_ids:
            raise InvariantError(
                "machine.resolution.invalid-topology", "topology chains must have unique chainId"
            )
        chain_ids.add(chain["chainId"])
        steps = chain.get("steps")
        if not isinstance(steps, list) or not steps:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                f"topology chain {chain['chainId']} must have at least one step",
            )
        for step in steps:
            if not isinstance(step, Mapping) or step.get("nodeId") not in chain_nodes:
                raise InvariantError(
                    "machine.resolution.invalid-topology",
                    f"topology chain {chain['chainId']} has an unknown node",
                )
        witnesses = chain.get("edges", ()) or ()
        if len(witnesses) != len(steps) - 1:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                "topology path must have one edge between adjacent steps",
            )
        for index, edge_id in enumerate(witnesses):
            if edge_id not in chain_edges:
                raise InvariantError(
                    "machine.resolution.invalid-topology",
                    f"topology chain {chain['chainId']} references an unknown edge",
                )
            edge = chain_edges[edge_id]
            if edge["from"] != steps[index]["nodeId"] or edge["to"] != steps[index + 1]["nodeId"]:
                raise InvariantError(
                    "machine.resolution.invalid-topology",
                    "topology path contains a discontinuous or reversed edge",
                )
        expected_floor = min(
            (chain_edges[identity]["resolution"] for identity in witnesses),
            key=resolution_rank,
            default="exact",
        )
        if chain.get("resolutionFloor") != expected_floor:
            raise InvariantError(
                "machine.resolution.invalid-topology",
                "topology path resolution must derive from its witnesses",
            )
        if "componentPath" in chain:
            node_index = {row["id"]: row for row in (method["nodes"] if method else nodes)}
            expected = _component_path(node_index, [step["nodeId"] for step in steps])
            if chain["componentPath"] != expected:
                raise InvariantError(
                    "machine.resolution.invalid-topology",
                    "topology component path must preserve its continuous method witnesses",
                )


# 验证顺序建立依赖：先身份、再引用、最后见证与链路。
# 独立验证器可检查已存储产物，无需启动分析提供者。
def validate_topology(value: Mapping[str, Any]) -> None:
    """Fail closed when a topology violates a materialized-graph invariant.

    Validate identities before references, then occurrence witnesses and ordered
    chains. Keeping this sequence explicit preserves the first actionable failure
    and prevents later checks from relying on unauthenticated graph structure.
    """

    if value.get("schemaVersion") != TOPOLOGY_SCHEMA_VERSION:
        raise InvariantError(
            "machine.resolution.invalid-topology",
            f"topology graph schemaVersion must be {TOPOLOGY_SCHEMA_VERSION}",
        )
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise InvariantError(
            "machine.resolution.invalid-topology", "topology nodes and edges must be lists"
        )
    ids = {item.get("id") for item in nodes if isinstance(item, Mapping)}
    if len(ids) != len(nodes) or None in ids:
        raise InvariantError(
            "machine.resolution.invalid-topology", "topology nodes must have unique IDs"
        )
    _validate_edges(edges, ids)
    method = value.get("methodGraph")
    _validate_method_graph(method)
    _validate_occurrences(value, edges)
    _validate_chains(value, nodes, edges, ids, method)


__all__ = ["validate_topology"]
