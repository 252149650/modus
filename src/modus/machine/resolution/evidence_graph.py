"""Private keyed evidence graph and deterministic analysis projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import networkx as nx

# Re-exported because callers historically treat the evidence graph as their
# entry point; the identity recipe itself lives with the other id recipes.
from modus.machine.store.ids import candidate_set_identity as candidate_set_identity

from ..errors import InvariantError

EVIDENCE_GRAPH_SCHEMA_VERSION = 1
COMMUNITY_PROJECTION_VERSION = "local-file-dependency"
COMMUNITY_RELATIONS = frozenset({"calls", "imports", "imports_from"})


# 规范序列化用于身份计算，兼容提取器携带的 Path 等元数据。
def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


# 编译器边优先保留其事实身份，结构边由端点、关系和位置派生。
def stable_edge_key(
    source: object,
    target: object,
    attributes: Mapping[str, Any],
    *,
    origin: str = "structural",
) -> str:
    """Return an idempotent key without embedding untrusted source text."""
    semantic_id = str(attributes.get("semantic_edge_id") or "")
    if semantic_id:
        return f"compiler:{semantic_id}"
    payload = {
        "source": str(source),
        "target": str(target),
        "relation": str(attributes.get("relation") or "related"),
        "sourceFile": str(attributes.get("source_file") or ""),
        "sourceLocation": str(attributes.get("source_location") or ""),
        "callSite": attributes.get("call_site") or attributes.get("callSite") or {},
        "origin": origin,
    }
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return f"{origin}:{digest[:32]}"


# 统一简单图和多重图的遍历接口，让消费者始终显式处理边键。
def iter_keyed_edges(
    graph: nx.Graph[str],
) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    """Yield a uniform keyed edge stream for simple and multi graphs."""
    if graph.is_multigraph():
        for source, target, key, data in cast("nx.MultiGraph[str]", graph).edges(
            keys=True, data=True
        ):
            yield source, target, str(key), data
        return
    for source, target, data in graph.edges(data=True):
        yield source, target, stable_edge_key(source, target, data), data


# 结构观察装入有向多重图，保留相同端点的不同发生证据。
# 端点缺失的边跳过，不创建没有来源属性的隐式节点。
def build_evidence_graph(
    extraction: Mapping[str, Any], *, root: Path | None = None
) -> nx.MultiDiGraph[str]:
    """Materialise structural extraction as a normalized evidence graph."""

    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph(
        evidenceGraphSchemaVersion=EVIDENCE_GRAPH_SCHEMA_VERSION
    )
    resolved_root = root.resolve() if root is not None else None

    def attributes(raw: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(raw)
        source_file = str(data.get("source_file") or "")
        if resolved_root and source_file and Path(source_file).is_absolute():
            try:
                data["source_file"] = (
                    Path(source_file).resolve().relative_to(resolved_root).as_posix()
                )
            except ValueError:
                data["external"] = True
        return data

    for raw in extraction.get("nodes", ()):
        if not isinstance(raw, Mapping) or not raw.get("id"):
            continue
        data = attributes(raw)
        node_id = str(data.pop("id"))
        graph.add_node(node_id, **data)
    declarations_by_name: dict[tuple[object, str], list[str]] = {}
    for node_id, data in graph.nodes(data=True):
        if data.get("signature") and data.get("kind") in {"function", "method"}:
            key = (
                data.get("source_file"),
                str(data.get("label", "")).removeprefix(".").removesuffix("()"),
            )
            declarations_by_name.setdefault(key, []).append(node_id)
    for raw in extraction.get("edges", ()):
        if not isinstance(raw, Mapping):
            continue
        source = str(raw.get("source", raw.get("from")))
        target = str(raw.get("target", raw.get("to")))
        if source not in graph or target not in graph:
            continue
        data = attributes(raw)
        for endpoint_field in ("source", "target", "from", "to", "key"):
            data.pop(endpoint_field, None)
        data.setdefault("source_file", graph.nodes[source].get("source_file", ""))
        if data.get("relation") == "calls":
            target_data = graph.nodes[target]
            key = (
                target_data.get("source_file"),
                str(target_data.get("label", "")).removeprefix(".").removesuffix("()"),
            )
            if len(declarations_by_name.get(key, [])) > 1:
                candidates = sorted(declarations_by_name[key])
                data["resolution"] = "candidate"
                data["candidateSetId"] = candidate_set_identity(candidates)
                data["candidateCount"] = len(candidates)
                data["reason_code"] = "overload.needs-semantic-binding"
        if not data.get("resolution"):
            data["resolution"] = (
                "candidate" if data.get("confidence") in {"INFERRED", "AMBIGUOUS"} else "exact"
            )
        data.setdefault(
            "binding_kind", "declared-exact" if data["resolution"] == "exact" else "heuristic"
        )
        edge_key = stable_edge_key(
            source, target, data, origin=str(data.get("_origin") or "structural")
        )
        graph.add_edge(source, target, key=edge_key, **data)
    graph.graph["diagnostics"] = extraction.get("diagnostics", [])
    return graph


# 聚类单位固定为文件，同文件声明数量不会改变依赖权重。
# 只使用精确且同上下文的本地依赖，候选关系不提升为架构事实。
def clustering_projection(graph: nx.Graph[str]) -> nx.Graph[str]:
    """Project exact local dependencies onto files, separated by context.

    Files are the smallest cross-language ownership envelope that remains
    stable when parsers discover more declarations. A file pair contributes
    once regardless of declaration count or call frequency; original symbol
    membership is retained only for mapping communities back to evidence.
    """
    projection: nx.Graph[str] = nx.Graph(projectionVersion=COMMUNITY_PROJECTION_VERSION)
    files: dict[tuple[str, str], list[str]] = {}
    for node_id, data in graph.nodes(data=True):
        path = str(data.get("source_file") or "")
        if not path or data.get("external") or data.get("topology_only"):
            continue
        context = str(data.get("analysis_context_id") or data.get("context_id") or "syntax")
        files.setdefault((context, path), []).append(node_id)
    owners: dict[str, str] = {}
    for (context, path), members in sorted(files.items()):
        identity = _canonical([context, path])
        projection_id = "file:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        projection.add_node(
            projection_id,
            path=path,
            context=context,
            members=tuple(sorted(members, key=str)),
        )
        owners.update((member, projection_id) for member in members)
    pairs: set[tuple[str, str]] = set()
    for source, target, _key, data in iter_keyed_edges(graph):
        if data.get("relation") not in COMMUNITY_RELATIONS or data.get("resolution") != "exact":
            continue
        left, right = owners.get(source), owners.get(target)
        if left is None or right is None or left == right:
            continue
        if projection.nodes[left]["context"] != projection.nodes[right]["context"]:
            continue
        pairs.add((left, right) if left < right else (right, left))
    projection.add_edges_from(sorted(pairs))
    return projection


# 边键必须在图内唯一归属一个端点对，防止证据身份发生碰撞。
# 强制多重有向图类型，避免平行边或方向在上游被静默丢失。
def validate_evidence_graph(graph: nx.Graph[str]) -> None:
    if not isinstance(graph, nx.MultiDiGraph):
        raise InvariantError("machine.resolution.multidigraph-required")
    seen: set[tuple[str, str, str]] = set()
    key_owners: dict[str, tuple[str, str]] = {}
    for source, target, key, _data in iter_keyed_edges(graph):
        identity = (str(source), str(target), key)
        if identity in seen:
            raise InvariantError("machine.resolution.duplicate-edge-key")
        seen.add(identity)
        owner = key_owners.setdefault(key, (str(source), str(target)))
        if owner != (str(source), str(target)):
            raise InvariantError("machine.resolution.edge-key-collision")


__all__ = [
    "COMMUNITY_PROJECTION_VERSION",
    "EVIDENCE_GRAPH_SCHEMA_VERSION",
    "build_evidence_graph",
    "candidate_set_identity",
    "clustering_projection",
    "iter_keyed_edges",
    "stable_edge_key",
    "validate_evidence_graph",
]
