"""Assign structural graph evidence to precise, explainable Scope candidates."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias, cast

import networkx as nx

from modus.machine.errors import InvariantError, safe_error_summary
from modus.machine.projection.scope_profiles import (
    StructuralBasis,
    build_candidate_profile,
    entry_role,
)
from modus.machine.resolution.evidence_graph import iter_keyed_edges

SCHEMA_VERSION = 1
CANDIDATES_SCHEMA_VERSION = SCHEMA_VERSION
NOTICE = (
    "Candidate skeletons are lightweight structural evidence from one source baseline; "
    "they do not define business domains or feature modules. Business names and ownership "
    "require source, business material, and user confirmation. A candidateId is valid only "
    "inside its analysis bundle and temporary confirmation artifacts such as "
    "scope-confirmation.md. Never persist it in a Module document; use the accompanying "
    "location (path/symbol/startLine) for durable provenance."
)

GroupKey: TypeAlias = tuple[str, str]
_RELATIONS = frozenset({"calls", "references", "imports", "imports_from"})


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parent_directory(relative: str) -> str:
    """Keep the complete parent path so fallback candidates do not collapse sibling areas."""

    parent = PurePosixPath(relative).parent.as_posix()
    return parent if parent not in {"", "/"} else "."


# 外部绝对路径不能进入候选文件身份，仓库内位置统一为相对路径。
def _relative(root: Path, value: object) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return ""
    return path.as_posix()


# 仅携带候选画像与后续展开需要的字段。
# 编译上下文和符号别名保留，避免压缩时失去精确归属依据。
def _compact_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the complete set of fields consumed by Scope summaries and expansion."""

    fields = (
        "entryId",
        "graphNodeId",
        "analysisContextId",
        "symbol",
        "symbolId",
        "symbolAliases",
        "language",
        "kind",
        "declarationName",
        "sourceRole",
        "signature",
        "displayName",
        "route",
        "contracts",
        "annotations",
        "location",
        "sourceDigest",
        "entryCandidate",
        "analysisCompleteness",
        "resolution",
        "sourceLayer",
    )
    return {field: entry[field] for field in fields if field in entry}


def _entry_sort_key(entry: Mapping[str, Any]) -> tuple[str, int, str, str]:
    location = entry.get("location")
    location = location if isinstance(location, Mapping) else {}
    return (
        str(location.get("path") or entry.get("sourceFile") or ""),
        int(location.get("startLine") or 0),
        str(entry.get("symbol") or ""),
        str(entry.get("entryId") or ""),
    )


# 别名扩充精确归属键，字符串本身不能被误当作可迭代别名列表。
# 所有者键用于信号归属匹配，不能仅以展示名称把多个入口合并。
def _entry_owner_keys(entry: Mapping[str, Any]) -> frozenset[str]:
    values = {
        str(entry.get("symbol") or ""),
        str(entry.get("displayName") or ""),
        str(entry.get("declarationName") or ""),
        str(entry.get("signature") or ""),
    }
    aliases = entry.get("symbolAliases")
    if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
        values.update(str(value) for value in aliases if value)
    return frozenset(value for value in values if value)


# 候选身份绑定构建分区、结构分组、文件和条目集合。
# 它属于当前分析基线，不能作为长期知识中的稳定模块身份。
def _candidate_identity(
    group_key: GroupKey, files: Sequence[str], entries: Sequence[dict[str, Any]]
) -> str:
    state = hashlib.sha256(b"scope-candidate\n")
    state.update(_canonical(["group", *group_key]).encode() + b"\n")
    for value in files:
        state.update(_canonical(["file", value]).encode() + b"\n")
    for entry in entries:
        state.update(_canonical(["entry", entry["entryId"]]).encode() + b"\n")
    return f"{group_key[0]}|{group_key[1]}:{state.hexdigest()[:16]}"


# 文件是跨语言稳定的结构归属单元，条目继承文件分组。
# 带独立分析上下文的观察不重复参与结构聚类。
# 社区票数相同按身份稳定消歧，缺失社区时回退完整目录路径。
# 只用精确关系计算图中心，候选边不能抬高结构重要性。
# 入口角色经过统一分类，测试与实现细节不会自动成为能力。
def _assign_candidate_membership(
    root: Path,
    graph: nx.MultiDiGraph[str],
    *,
    communities: Mapping[str, list[str]],
    source_digests: Mapping[str, str],
    build_unit_assignments: Mapping[str, tuple[tuple[str, ...], str]],
    projected_entries_by_path: Mapping[str, Sequence[Mapping[str, Any]]],
    source_roles: Mapping[str, str],
) -> tuple[
    dict[GroupKey, set[str]],
    dict[GroupKey, list[dict[str, Any]]],
    dict[str, GroupKey],
    dict[str, set[GroupKey]],
    Counter[str],
    list[tuple[str, str, str]],
    list[dict[str, Any]],
]:
    """Assign files and entries to stable, build-unit-local candidate boundaries.

    The clustering projection already collapses declarations onto files before
    Louvain partitioning. A source file is the smallest stable ownership
    envelope available across every supported language, so every legal entry
    in that file inherits the same build-unit-local community. The dominant
    vote is retained as a defensive adapter for older or partial projections;
    directory grouping is the deterministic fallback when no community
    evidence exists.
    """

    unit_assignments = build_unit_assignments
    # 社区成员索引只服务结构分组，不命名业务模块。
    # 各组同时绑定主构建单元，避免跨编译边界的偶然聚合。
    community_by_node = {
        str(node): community for community, nodes in communities.items() for node in nodes
    }
    nodes_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    node_files: dict[str, str] = {}
    group_by_node: dict[str, GroupKey] = {}
    files_by_group: dict[GroupKey, set[str]] = defaultdict(set)
    groups_by_file: dict[str, set[GroupKey]] = defaultdict(set)
    for node, raw_attributes in graph.nodes(data=True):
        attributes = raw_attributes
        if attributes.get("analysis_context_id") or attributes.get("context_id"):
            continue
        relative = _relative(root, attributes.get("source_file"))
        if not relative or relative not in source_digests:
            continue
        if relative not in projected_entries_by_path:
            continue
        identity = str(node)
        nodes_by_file[relative].append({**attributes, "id": identity})
        node_files[identity] = relative

    for relative, nodes in sorted(nodes_by_file.items()):
        community_votes = Counter(
            community_by_node.get(str(node.get("id") or ""), "") for node in nodes
        )
        community_votes.pop("", None)
        structural_group = (
            min(community_votes, key=lambda value: (-community_votes[value], value))
            if community_votes
            else "directory:" + _parent_directory(relative)
        )
        group_key = (unit_assignments[relative][1], structural_group)
        files_by_group[group_key].add(relative)
        groups_by_file[relative].add(group_key)
        for node_row in nodes:
            group_by_node[str(node_row["id"])] = group_key

    # 只在已知源码节点之间统计精确边。
    # 外部端点和未解析调用不参与候选耦合强度。
    exact_edges: list[tuple[str, str, str]] = []
    exact_degrees: Counter[str] = Counter()
    for source, target, _key, raw_attributes in iter_keyed_edges(graph):
        attributes = raw_attributes
        source_id, target_id = str(source), str(target)
        relative = node_files.get(source_id, "")
        if not relative:
            continue
        relation = str(attributes.get("relation") or "")
        if (
            relation in _RELATIONS
            and attributes.get("resolution") == "exact"
            and target_id in node_files
        ):
            exact_edges.append((source_id, target_id, relation))
            exact_degrees[source_id] += 1
            exact_degrees[target_id] += 1

    candidate_entries: dict[GroupKey, list[dict[str, Any]]] = defaultdict(list)
    gaps: list[dict[str, Any]] = []
    for relative, _nodes in sorted(nodes_by_file.items()):
        try:
            projection_entries = projected_entries_by_path.get(relative, [])
            for raw_entry in projection_entries:
                entry = {
                    **_compact_entry(raw_entry),
                    "sourceRole": source_roles.get(relative, "runtime"),
                    "primaryBuildUnitId": unit_assignments[relative][1],
                    "resolution": str(raw_entry.get("resolution") or "exact"),
                    "sourceLayer": str(raw_entry.get("sourceLayer") or "cst"),
                    "sourceFile": relative,
                }
                role = entry_role(entry)
                if role is None:
                    continue
                file_groups = groups_by_file.get(relative, set())
                group_key = (
                    next(iter(file_groups))
                    if len(file_groups) == 1
                    else (
                        unit_assignments[relative][1],
                        "directory:" + _parent_directory(relative),
                    )
                )
                entry["entryRole"] = role
                candidate_entries[group_key].append(entry)
                files_by_group[group_key].add(relative)
                groups_by_file[relative].add(group_key)
        except (OSError, UnicodeError, ValueError) as exc:
            gaps.append(
                {
                    "path": relative,
                    "reasonCode": "machine.projection.scope-entry-unavailable",
                    "summary": safe_error_summary(exc),
                }
            )

    return (
        files_by_group,
        candidate_entries,
        group_by_node,
        groups_by_file,
        exact_degrees,
        exact_edges,
        gaps,
    )


# 信号先按文件和拥有者联合匹配，避免跨文件同名符号误归属。
# 只有一个文件候选时才允许缺少拥有者的回退。
# 多候选歧义计入未绑定数，没有候选则生成独立覆盖缺口。
def _assign_authoring_signals(
    candidate_entries: Mapping[GroupKey, Sequence[Mapping[str, Any]]],
    groups_by_file: Mapping[str, set[GroupKey]],
    authoring_records_by_path: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[
    dict[GroupKey, list[Mapping[str, Any]]],
    Counter[GroupKey],
    list[dict[str, Any]],
]:
    """Bind signals only when their owner or sole file candidate is unambiguous."""

    owner_groups: dict[tuple[str, str], set[GroupKey]] = defaultdict(set)
    for group_key, entries in candidate_entries.items():
        for entry in entries:
            path = str(entry.get("sourceFile") or "")
            for owner_key in _entry_owner_keys(entry):
                owner_groups[path, owner_key].add(group_key)

    records_by_group: dict[GroupKey, list[Mapping[str, Any]]] = defaultdict(list)
    unbound_by_group: Counter[GroupKey] = Counter()
    unbound_gaps: list[dict[str, Any]] = []
    candidate_group_keys = set(candidate_entries)
    for path, records in sorted(authoring_records_by_path.items()):
        file_groups = groups_by_file.get(path, set()).intersection(candidate_group_keys)
        for record in records:
            owner = str(record.get("symbol") or "")
            exact_groups = owner_groups.get((path, owner), set()) if owner else set()
            if len(exact_groups) == 1:
                records_by_group[next(iter(exact_groups))].append(record)
            elif not exact_groups and len(file_groups) == 1:
                records_by_group[next(iter(file_groups))].append(record)
            else:
                relevant_groups = exact_groups or file_groups
                for group_key in relevant_groups:
                    unbound_by_group[group_key] += 1
                if not relevant_groups:
                    unbound_gaps.append(
                        {
                            "path": path,
                            "reasonCode": "machine.projection.scope-signal-owner-unbound",
                            "evidenceKind": str(record.get("evidenceKind") or ""),
                        }
                    )
    return records_by_group, unbound_by_group, unbound_gaps


# 候选 ID 与条目身份均需唯一，禁止多个候选争用同一入口。
# 代表样本必须属于完整条目清单，不能独立伪造摘要。
# 动作、声明、类型分布与文件角色计数逐项核对。
# 这些是不变量检查，失败表示投影实现错误而非普通源码降级。
# 发布候选前验证成员与代表材料一致，避免摘要声称不存在的证据。
def _validate_candidates(
    candidates: Sequence[Mapping[str, Any]], source_roles: Mapping[str, str]
) -> None:
    candidate_ids = [str(candidate.get("candidateId") or "") for candidate in candidates]
    if any(not candidate_id for candidate_id in candidate_ids) or len(candidate_ids) != len(
        set(candidate_ids)
    ):
        raise InvariantError("machine.projection.scope-candidate-id")

    owned_entry_ids: set[str] = set()
    for candidate in candidates:
        entries = [
            cast(Mapping[str, Any], entry)
            for entry in candidate.get("entries", [])
            if isinstance(entry, Mapping)
        ]
        entry_ids = [str(entry.get("entryId") or "") for entry in entries]
        if any(not entry_id for entry_id in entry_ids) or len(entry_ids) != len(set(entry_ids)):
            raise InvariantError("machine.projection.scope-entry-ownership")
        if owned_entry_ids.intersection(entry_ids):
            raise InvariantError("machine.projection.scope-entry-ownership")
        owned_entry_ids.update(entry_ids)

        representatives = [
            cast(Mapping[str, Any], entry)
            for entry in candidate.get("representativeEntries", [])
            if isinstance(entry, Mapping)
        ]
        representative_ids = {str(entry.get("entryId") or "") for entry in representatives}
        if not representative_ids.issubset(entry_ids):
            raise InvariantError("machine.projection.scope-representative-ownership")

        inventory = candidate.get("inventory")
        if not isinstance(inventory, Mapping):
            raise InvariantError("machine.projection.scope-inventory")
        action_count = sum(entry.get("entryRole") == "action" for entry in entries)
        declaration_count = sum(entry.get("entryRole") == "declaration" for entry in entries)
        if (
            inventory.get("actionEntryCount") != action_count
            or inventory.get("declarationEntryCount") != declaration_count
            or inventory.get("representativeEntryCount") != len(representatives)
        ):
            raise InvariantError("machine.projection.scope-inventory")
        expected_kinds = dict(
            sorted(Counter(str(entry.get("kind") or "unknown") for entry in entries).items())
        )
        if inventory.get("entryKindCounts") != expected_kinds:
            raise InvariantError("machine.projection.scope-inventory")
        expected_action_kinds = dict(
            sorted(
                Counter(
                    str(entry.get("kind") or "unknown")
                    for entry in entries
                    if entry.get("entryRole") == "action"
                ).items()
            )
        )
        if inventory.get("actionEntryKindCounts") != expected_action_kinds:
            raise InvariantError("machine.projection.scope-inventory")
        files = [str(path) for path in candidate.get("files", []) if isinstance(path, str)]
        roles = Counter(source_roles.get(path, "runtime") for path in files)
        if inventory.get("runtimeFileCount") != roles["runtime"] or inventory.get(
            "supportFileCount"
        ) != sum(count for role, count in roles.items() if role != "runtime"):
            raise InvariantError("machine.projection.scope-inventory")


# 外部接口和异步入口优先于普通资源与图中心线索。
# 入口数量只在同级信号内排序，规模不能替代语义证据。
# 候选身份作为最终排序键，保证有限预算下首屏稳定。
# 候选优先级只决定展示顺序，不改变完整候选的身份或成员集合。
def _candidate_priority(candidate: Mapping[str, Any]) -> tuple[int, int, str]:
    capability = candidate.get("capabilitySignals")
    capability = capability if isinstance(capability, Mapping) else {}
    entry_signals = capability.get("entries")
    entry_signals = entry_signals if isinstance(entry_signals, Mapping) else {}
    signal_names = {str(name) for name in entry_signals}
    if signal_names.intersection({"external-http", "external-rpc", "cli"}):
        rank = 0
    elif signal_names.intersection({"job", "mq"}):
        rank = 1
    elif capability.get("effects") or capability.get("resources"):
        rank = 2
    elif any(
        isinstance(entry, Mapping) and entry.get("graphCenter")
        for entry in candidate.get("representativeEntries", [])
    ):
        rank = 3
    else:
        rank = 4
    inventory = candidate.get("inventory")
    inventory = inventory if isinstance(inventory, Mapping) else {}
    entry_count = int(inventory.get("actionEntryCount") or 0) + int(
        inventory.get("declarationEntryCount") or 0
    )
    return rank, -entry_count, str(candidate.get("candidateId") or "")


# 结构候选消费已投影入口，不在此重新打开源码。
# 缺省构建分区按完整父目录建立，避免兄弟区域被合并。
# 只有包含动作入口的分组才形成候选，声明用于丰富已有边界。
# 候选明细完整保留，摘要只做有界取样。
# 发布前统一校验归属与计数，输出状态由实际缺口决定。
def project_scope_candidates(
    root: Path,
    graph: nx.MultiDiGraph[str],
    *,
    communities: Mapping[str, list[str]],
    source_digests: Mapping[str, str],
    baseline_digest: str,
    source_roles: Mapping[str, str] | None = None,
    build_unit_assignments: Mapping[str, tuple[tuple[str, ...], str]] | None = None,
    projected_entries_by_path: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    authoring_records_by_path: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    coverage_gaps_by_path: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Return complete candidate detail plus bounded, deterministic profiles."""

    roles_by_path = source_roles or {path: "runtime" for path in source_digests}
    assignments = build_unit_assignments or {
        path: ((f"structural:{_parent_directory(path)}",), f"structural:{_parent_directory(path)}")
        for path in source_digests
    }
    entries_by_path = projected_entries_by_path or {}
    (
        files_by_group,
        candidate_entries,
        group_by_node,
        groups_by_file,
        exact_degrees,
        exact_edges,
        projection_gaps,
    ) = _assign_candidate_membership(
        root,
        graph,
        communities=communities,
        source_digests=source_digests,
        build_unit_assignments=assignments,
        projected_entries_by_path=entries_by_path,
        source_roles=roles_by_path,
    )
    authoring_records, unbound_signals, unbound_signal_gaps = _assign_authoring_signals(
        candidate_entries,
        groups_by_file,
        authoring_records_by_path or {},
    )
    # 投影失败与规则归属失败合并保留，再附加文件自身取证缺口。
    # 最终候选按其文件集合继承相关缺口数量。
    coverage_gaps: list[dict[str, Any]] = [*projection_gaps, *unbound_signal_gaps]
    for path, gaps in sorted((coverage_gaps_by_path or {}).items()):
        coverage_gaps.extend({"path": path, **dict(gap)} for gap in gaps)

    candidates: list[dict[str, Any]] = []
    candidate_id_by_group: dict[GroupKey, str] = {}
    for group_key, raw_entries in sorted(candidate_entries.items()):
        entries = sorted((dict(entry) for entry in raw_entries), key=_entry_sort_key)
        # DTOs, enums, namespaces, and other declarations enrich an existing
        # action-bearing boundary; they do not become candidates themselves.
        if not any(entry.get("entryRole") == "action" for entry in entries):
            continue
        # 中心标记选取当前候选内最高精确连接度。
        # 完全没有连接时不把所有零度节点标为中心。
        highest_degree = max(
            (exact_degrees[str(entry.get("graphNodeId") or "")] for entry in entries),
            default=0,
        )
        for entry in entries:
            entry["graphCenter"] = bool(
                highest_degree
                and exact_degrees[str(entry.get("graphNodeId") or "")] == highest_degree
            )
        files = sorted(files_by_group[group_key])
        roles = Counter(roles_by_path.get(path, "runtime") for path in files)
        primary_unit, structural_group = group_key
        grouping: Literal["community", "directory"] = (
            "community" if structural_group.startswith("community:") else "directory"
        )
        structural_basis = StructuralBasis(
            grouping=grouping,
            community=structural_group if grouping == "community" else None,
            directories=tuple(sorted({_parent_directory(path) for path in files})[:16]),
        )
        profile = build_candidate_profile(
            structural_basis=structural_basis,
            entries=entries,
            runtime_file_count=roles["runtime"],
            support_file_count=sum(count for role, count in roles.items() if role != "runtime"),
            authoring_records=authoring_records.get(group_key, []),
            unbound_signal_count=unbound_signals[group_key],
        )
        candidate_id = _candidate_identity(group_key, files, entries)
        candidate_id_by_group[group_key] = candidate_id
        profile_fields = profile.to_dict()
        candidate_gaps = [gap for gap in coverage_gaps if str(gap.get("path") or "") in files]
        candidates.append(
            {
                "candidateId": candidate_id,
                "partition": primary_unit,
                "buildUnitId": primary_unit,
                **profile_fields,
                "coverageGapCount": len(candidate_gaps),
                "files": files,
                "entries": entries,
            }
        )

    # 跨候选耦合按无向候选对聚合数量，关系种类仍保留。
    # 组内连接不重复输出为跨边界关系。
    couplings: Counter[tuple[str, str, str]] = Counter()
    for source, target, relation in exact_edges:
        left = candidate_id_by_group.get(group_by_node.get(source, ("", "")))
        right = candidate_id_by_group.get(group_by_node.get(target, ("", "")))
        if left and right and left != right:
            first, second = sorted((left, right))
            couplings[first, second, relation] += 1
    coupling_records = [
        {"between": [left, right], "relation": relation, "edges": count}
        for (left, right, relation), count in sorted(
            couplings.items(), key=lambda item: (-item[1], item[0])
        )
    ]

    # 校验在排序与发布前执行，防止有问题的候选进入持久化目录。
    # 覆盖统计基于最终候选，不以原始图节点数代替入口数。
    _validate_candidates(candidates, roles_by_path)
    ordered_candidates = sorted(candidates, key=_candidate_priority)
    entry_count = sum(len(candidate["entries"]) for candidate in ordered_candidates)
    return {
        "schemaVersion": CANDIDATES_SCHEMA_VERSION,
        "baselineDigest": baseline_digest,
        "notice": NOTICE,
        "candidates": ordered_candidates,
        "crossCandidateCoupling": coupling_records,
        "uncovered": sorted({str(gap.get("path")) for gap in coverage_gaps if gap.get("path")}),
        "coverage": {
            "files": len(source_digests),
            "entries": entry_count,
            "structuralCommunitiesAvailable": any(
                candidate["structuralBasis"]["grouping"] == "community"
                for candidate in ordered_candidates
            ),
        },
        "coverageGaps": coverage_gaps,
        "status": "partial" if coverage_gaps else "ready" if candidates else "empty",
    }


__all__ = ["CANDIDATES_SCHEMA_VERSION", "NOTICE", "project_scope_candidates"]
