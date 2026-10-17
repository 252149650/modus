"""Explainable, cycle-safe navigation steps for candidate investigation."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import networkx as nx

from modus.machine.resolution.evidence_graph import iter_keyed_edges
from modus.paths import ModusPaths

_NAVIGATION_RELATIONS = frozenset({"calls", "references"})


# 绝对路径必须处于项目根内，否则不能作为公开导航位置。
# 相对路径保持项目坐标表达，后续可与候选文件表直接匹配。
def _relative(paths: ModusPaths, value: object) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.relative_to(paths.root).as_posix()
        except ValueError:
            return ""
    return path.as_posix()


# 强连通分量身份同时绑定候选和排序后的成员集合。
# 分量枚举顺序改变不会导致导航身份漂移。
def _component_id(candidate_id: str, members: list[str]) -> str:
    digest = hashlib.sha256(
        (candidate_id + "\0" + "\0".join(sorted(members))).encode()
    ).hexdigest()[:24]
    return f"scc.{digest}"


# 导航是已有证据图的投影，不重新解析调用关系。
# 只压缩同一候选内部的精确边，避免用猜测关系合并模块。
# 跨候选连接与不确定连接都作为终止边交给阅读者判断。
def project_navigation(
    paths: ModusPaths,
    graph: Any,
    *,
    scope: Mapping[str, Any],
    source_digests: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Compress exact local cycles and retain cross-candidate terminal edges."""

    candidate_by_path: dict[str, str] = {}
    for raw in scope.get("candidates", []):
        if not isinstance(raw, Mapping):
            continue
        candidate_id = str(raw.get("candidateId") or "")
        for value in raw.get("files", []):
            if candidate_id and isinstance(value, str):
                candidate_by_path[value] = candidate_id
    # 节点标签优先使用限定名，位置同时保留源码摘要。
    # 摘要让调用方能够核验导航与当前源码是否属于同一版本。
    node_paths: dict[str, str] = {}
    labels: dict[str, str] = {}
    lines: dict[str, int | None] = {}
    for node, attrs in graph.nodes(data=True):
        identity = str(node)
        path = _relative(paths, attrs.get("source_file"))
        node_paths[identity] = path
        labels[identity] = str(attrs.get("qualified_name") or attrs.get("label") or identity)
        line = attrs.get("start_line") or attrs.get("line")
        lines[identity] = line if isinstance(line, int) and not isinstance(line, bool) else None

    # 先按候选边界与置信度分组，再构造局部有向图。
    # 没有来源候选的边不属于本次导航范围。
    local: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    cross: list[tuple[str, str, str, str]] = []
    uncertain: list[tuple[str, str, str, str, str]] = []
    for source, target, _key, attrs in iter_keyed_edges(graph):
        relation = str(attrs.get("relation") or "")
        resolution = str(attrs.get("resolution") or "")
        if relation not in _NAVIGATION_RELATIONS:
            continue
        source_id, target_id = str(source), str(target)
        left = candidate_by_path.get(node_paths.get(source_id, ""))
        right = candidate_by_path.get(node_paths.get(target_id, ""))
        if not left:
            continue
        if resolution != "exact":
            uncertain.append((left, right or "", source_id, target_id, resolution))
        elif left == right:
            local[left].append((source_id, target_id))
        elif right:
            cross.append((left, right, source_id, target_id))
        else:
            uncertain.append((left, "", source_id, target_id, "exact"))

    result: list[dict[str, Any]] = []
    for candidate_id, edges in sorted(local.items()):
        candidate_graph: nx.DiGraph[str] = nx.DiGraph()
        candidate_graph.add_edges_from(edges)
        # 强连通分量收敛递归和互相调用，避免导航无限展开。
        # 成员排序同时决定稳定身份与可复现的代表锚点。
        components = [
            sorted(str(member) for member in component)
            for component in nx.strongly_connected_components(candidate_graph)
        ]
        component_by_node: dict[str, str] = {}
        members_by_component: dict[str, list[str]] = {}
        for members in sorted(components, key=lambda item: item):
            component_id = _component_id(candidate_id, members)
            members_by_component[component_id] = members
            component_by_node.update({member: component_id for member in members})
            # 单节点只有存在自环才需要输出循环说明。
            # 代表符号数量受限，但 memberCount 保留完整分量规模。
            if len(members) > 1 or candidate_graph.has_edge(members[0], members[0]):
                anchor_node = members[0]
                path = node_paths.get(anchor_node, "")
                result.append(
                    {
                        "recordType": "navigationComponent",
                        "candidateIds": [candidate_id],
                        "componentId": component_id,
                        "memberCount": len(members),
                        "representativeSymbols": [labels[item] for item in members[:8]],
                        "cyclic": True,
                        "path": path,
                        "symbol": labels.get(anchor_node, ""),
                        "startLine": lines.get(anchor_node),
                        "sourceDigest": source_digests.get(path, ""),
                    }
                )
        # 分量之间的边构成压缩后的导航图，分量内部边已由循环说明覆盖。
        # 去重避免多条原始调用产生重复的阅读步骤。
        condensed_edges = {
            (component_by_node[source], component_by_node[target])
            for source, target in edges
            if component_by_node[source] != component_by_node[target]
        }
        for source_component, target_component in sorted(condensed_edges):
            source_node = members_by_component[source_component][0]
            target_node = members_by_component[target_component][0]
            path = node_paths.get(source_node, "")
            result.append(
                {
                    "recordType": "navigationStep",
                    "candidateIds": [candidate_id],
                    "sourceComponentId": source_component,
                    "targetComponentId": target_component,
                    "sourceSymbol": labels.get(source_node, ""),
                    "targetSymbol": labels.get(target_node, ""),
                    "resolution": "exact",
                    "terminal": False,
                    "path": path,
                    "symbol": labels.get(source_node, ""),
                    "startLine": lines.get(source_node),
                    "sourceDigest": source_digests.get(path, ""),
                }
            )
    for left, right, source, target in sorted(set(cross)):
        path = node_paths.get(source, "")
        result.append(
            {
                "recordType": "navigationStep",
                "candidateIds": [left, right],
                "sourceSymbol": labels.get(source, ""),
                "targetSymbol": labels.get(target, ""),
                "resolution": "exact",
                "terminal": True,
                "boundaryCandidateId": right,
                "path": path,
                "symbol": labels.get(source, ""),
                "startLine": lines.get(source),
                "sourceDigest": source_digests.get(path, ""),
            }
        )
    # 非精确关系仍有导航价值，但不能继续自动穿透。
    # 未知置信度统一降为 unresolved，防止扩展字段被误认为精确事实。
    for left, right, source, target, resolution in sorted(set(uncertain)):
        path = node_paths.get(source, "")
        result.append(
            {
                "recordType": "navigationStep",
                "candidateIds": [left, *([right] if right and right != left else [])],
                "sourceSymbol": labels.get(source, ""),
                "targetSymbol": labels.get(target, target),
                "resolution": resolution
                if resolution in {"exact", "candidate", "heuristic", "unresolved"}
                else "unresolved",
                "terminal": True,
                **({"boundaryCandidateId": right} if right and right != left else {}),
                "path": path,
                "symbol": labels.get(source, ""),
                "startLine": lines.get(source),
                "sourceDigest": source_digests.get(path, ""),
            }
        )
    return result


__all__ = ["project_navigation"]
