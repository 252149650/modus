"""Typed capability profiles for precision-first Scope candidates.

This module deliberately knows nothing about graph partitioning or bundle
storage.  It turns already-owned entries and authoring signals into a compact,
deterministic description that can be sampled in summaries without discarding
the complete candidate inventory.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from modus.machine.store.contracts import JsonObject

from ..errors import InvariantError

EntryRole = Literal["action", "declaration"]

_DECLARATION_KINDS = frozenset(
    {"type", "class", "interface", "enum", "namespace", "package", "module"}
)
_NON_CAPABILITY_KINDS = frozenset(
    {
        "file",
        "reference",
        "field",
        "property",
        "variable",
        "parameter",
        "configuration",
        "data-definition",
    }
)
_REPRESENTATIVE_LIMIT = 6
_RESOLUTION_ORDER = {"exact": 0, "candidate": 1, "heuristic": 2, "unresolved": 3}


# 库存计数来自完整条目，代表样本数量不能替代真实规模。
@dataclass(frozen=True, slots=True)
class CandidateInventory:
    """Counts whose consistency is enforced before a Scope bundle is published."""

    runtime_file_count: int
    support_file_count: int
    action_entry_count: int
    declaration_entry_count: int
    representative_entry_count: int
    entry_kind_counts: tuple[tuple[str, int], ...]
    action_entry_kind_counts: tuple[tuple[str, int], ...]

    def to_dict(self) -> JsonObject:
        return {
            "runtimeFileCount": self.runtime_file_count,
            "supportFileCount": self.support_file_count,
            "actionEntryCount": self.action_entry_count,
            "declarationEntryCount": self.declaration_entry_count,
            "representativeEntryCount": self.representative_entry_count,
            "entryKindCounts": {key: value for key, value in self.entry_kind_counts},
            "actionEntryKindCounts": {key: value for key, value in self.action_entry_kind_counts},
        }


# 社区和目录只解释候选形成依据，不直接命名业务领域。
@dataclass(frozen=True, slots=True)
class StructuralBasis:
    """Machine-observed structure used to form a candidate, never a business label."""

    grouping: Literal["community", "directory"]
    community: str | None
    directories: tuple[str, ...]

    def to_dict(self) -> JsonObject:
        return {
            "grouping": self.grouping,
            "community": self.community,
            "directories": list(self.directories),
        }


# 信号按开放的证据种类统计，新增规则包无需改写固定业务枚举。
@dataclass(frozen=True, slots=True)
class CapabilitySignals:
    """Counts of extensible authoring evidence, grouped only by evidence contract."""

    entries: tuple[tuple[str, int], ...]
    effects: tuple[tuple[str, int], ...]
    resources: tuple[tuple[str, int], ...]
    unbound_signal_count: int

    def to_dict(self) -> JsonObject:
        return {
            "entries": {key: value for key, value in self.entries},
            "effects": {key: value for key, value in self.effects},
            "resources": {key: value for key, value in self.resources},
            "unboundSignalCount": self.unbound_signal_count,
        }


# 摘要有界但明细无损，有限首屏不会删除后续可展开的入口。
@dataclass(frozen=True, slots=True)
class CandidateProfile:
    """Bounded summary data derived from a candidate's lossless detail records."""

    structural_basis: StructuralBasis
    inventory: CandidateInventory
    capability_signals: CapabilitySignals
    representative_entries: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> JsonObject:
        return cast(
            JsonObject,
            {
                "structuralBasis": self.structural_basis.to_dict(),
                "inventory": self.inventory.to_dict(),
                "capabilitySignals": self.capability_signals.to_dict(),
                "representativeEntries": [dict(entry) for entry in self.representative_entries],
            },
        )


def _text(entry: Mapping[str, Any]) -> str:
    return " ".join(
        (
            str(entry.get("kind") or ""),
            str(entry.get("symbol") or ""),
            str(entry.get("displayName") or ""),
            str(entry.get("route") or ""),
            " ".join(str(value) for value in entry.get("contracts") or ()),
            " ".join(str(value) for value in entry.get("annotations") or ()),
        )
    ).casefold()


# 构造器描述对象创建，不作为独立业务动作入口。
# Java 方法形状的构造器也需识别，不能只依赖 kind 字段。
def _is_constructor(entry: Mapping[str, Any]) -> bool:
    kind = str(entry.get("kind") or "").casefold()
    name = str(entry.get("declarationName") or entry.get("displayName") or "")
    symbol = str(entry.get("symbol") or "")
    if kind == "constructor":
        return True
    if name in {"<init>", "__init__", "constructor"} or "#<init>" in symbol:
        return True
    if str(entry.get("language") or "").casefold() != "java" or kind != "method":
        return False
    qualified = symbol.split("#", 1)[-1].split("(", 1)[0]
    parts = qualified.rsplit(".", 2)
    if name and len(parts) >= 2 and parts[-1] == name and parts[-2] == name:
        return True
    owner = symbol.split("#", 1)[0].rsplit(".", 1)[-1]
    return bool(name and "#" in symbol and owner == name and qualified == name)


# 只有 runtime 材料中的合法入口进入能力库存。
# 类型声明可补充已有候选，但不能被计入动作数量。
def entry_role(entry: Mapping[str, Any]) -> EntryRole | None:
    """Classify legal Scope entries while excluding implementation-only declarations."""

    kind = str(entry.get("kind") or "").casefold()
    if (
        str(entry.get("sourceRole") or "") != "runtime"
        or kind in _NON_CAPABILITY_KINDS
        or _is_constructor(entry)
    ):
        return None
    if bool(entry.get("entryCandidate")):
        return "action"
    if kind in _DECLARATION_KINDS:
        return "declaration"
    return None


# 动作与高精度证据优先，路径和位置提供稳定同分顺序。
def _entry_order(entry: Mapping[str, Any]) -> tuple[int, int, str, int, str, str]:
    location = entry.get("location")
    location = location if isinstance(location, Mapping) else {}
    return (
        0 if entry.get("entryRole") == "action" else 1,
        _RESOLUTION_ORDER.get(str(entry.get("resolution") or "unresolved"), 3),
        str(location.get("path") or entry.get("sourceFile") or ""),
        int(location.get("startLine") or 0),
        str(entry.get("symbol") or entry.get("displayName") or ""),
        str(entry.get("entryId") or ""),
    )


# 主题只用于样本多样性选择，不生成新的执行效果事实。
# 图中心是辅助信号，仍需与入口角色一起解释。
def _facets(entry: Mapping[str, Any]) -> frozenset[str]:
    evidence_text = _text(entry)
    facets: set[str] = set()
    if any(token in evidence_text for token in ("http", "rpc", "route", "api", "mapping")):
        facets.add("public-contract")
    if any(
        token in evidence_text
        for token in ("job", "scheduled", "message", "listener", "callback", "event")
    ):
        facets.add("asynchronous")
    if any(
        token in evidence_text
        for token in (
            "write",
            "save",
            "insert",
            "update",
            "delete",
            "commit",
            "publish",
            "send",
            "state",
            "effect",
        )
    ):
        facets.add("state-change")
    if entry.get("graphCenter"):
        facets.add("graph-center")
    facets.add("declaration" if entry.get("entryRole") == "declaration" else "action")
    return frozenset(facets)


# 依次覆盖契约、异步、状态变化等观察面，避免只截取前六条。
# 同一入口只能占一个样本位置，剩余配额再按稳定顺序补齐。
# 代表样本始终来自传入条目，不根据名称构造新入口。
def select_representative_entries(
    entries: Sequence[Mapping[str, Any]], *, limit: int = _REPRESENTATIVE_LIMIT
) -> tuple[Mapping[str, Any], ...]:
    """Choose a bounded, coverage-oriented sample instead of the first N entries."""

    if limit < 0:
        raise InvariantError("machine.projection.scope-representative-limit")
    ordered = sorted(entries, key=_entry_order)
    chosen: list[Mapping[str, Any]] = []
    selected_ids: set[str] = set()
    for facet in (
        "public-contract",
        "asynchronous",
        "state-change",
        "graph-center",
        "action",
        "declaration",
    ):
        match = next(
            (
                entry
                for entry in ordered
                if str(entry.get("entryId") or "") not in selected_ids and facet in _facets(entry)
            ),
            None,
        )
        if match is not None:
            chosen.append(match)
            selected_ids.add(str(match.get("entryId") or ""))
        if len(chosen) == limit:
            return tuple(chosen)
    for entry in ordered:
        identity = str(entry.get("entryId") or "")
        if identity in selected_ids:
            continue
        chosen.append(entry)
        selected_ids.add(identity)
        if len(chosen) == limit:
            break
    return tuple(chosen)


# 未知或缺少种类的信号计入未绑定数量，不能悄然丢弃。
# 三种主要信号轴保持独立，便于描述入口、效果与资源。
def capability_signals(
    records: Sequence[Mapping[str, Any]], *, unbound_signal_count: int = 0
) -> CapabilitySignals:
    """Aggregate current and future rule-pack signals without a closed taxonomy."""

    counters: dict[str, Counter[str]] = {
        "entry": Counter(),
        "effect": Counter(),
        "resource": Counter(),
    }
    unknown = 0
    for record in records:
        kind = str(record.get("kind") or "")
        evidence_kind = str(record.get("evidenceKind") or "")
        if kind not in counters or not evidence_kind:
            unknown += 1
            continue
        counters[kind][evidence_kind] += 1
    return CapabilitySignals(
        entries=tuple(sorted(counters["entry"].items())),
        effects=tuple(sorted(counters["effect"].items())),
        resources=tuple(sorted(counters["resource"].items())),
        unbound_signal_count=unbound_signal_count + unknown,
    )


# 统计和样本由同一份完整入口集合生成。
# 运行文件与支持文件分别计数，测试材料不能抬高运行能力规模。
# 冻结后的摘要供序列化层复用，避免各视图重复推导不一致数字。
# 信号归属由上游负责，本层只聚合已经分配的观察。
def build_candidate_profile(
    *,
    structural_basis: StructuralBasis,
    entries: Sequence[Mapping[str, Any]],
    runtime_file_count: int,
    support_file_count: int,
    authoring_records: Sequence[Mapping[str, Any]],
    unbound_signal_count: int,
) -> CandidateProfile:
    """Build the immutable summary profile from one candidate's owned evidence."""

    representatives = select_representative_entries(entries)
    role_counts = Counter(str(entry.get("entryRole") or "") for entry in entries)
    kind_counts = Counter(str(entry.get("kind") or "unknown") for entry in entries)
    action_kind_counts = Counter(
        str(entry.get("kind") or "unknown")
        for entry in entries
        if entry.get("entryRole") == "action"
    )
    inventory = CandidateInventory(
        runtime_file_count=runtime_file_count,
        support_file_count=support_file_count,
        action_entry_count=role_counts["action"],
        declaration_entry_count=role_counts["declaration"],
        representative_entry_count=len(representatives),
        entry_kind_counts=tuple(sorted(kind_counts.items())),
        action_entry_kind_counts=tuple(sorted(action_kind_counts.items())),
    )
    return CandidateProfile(
        structural_basis=structural_basis,
        inventory=inventory,
        capability_signals=capability_signals(
            authoring_records, unbound_signal_count=unbound_signal_count
        ),
        representative_entries=representatives,
    )


__all__ = [
    "CandidateInventory",
    "CandidateProfile",
    "CapabilitySignals",
    "EntryRole",
    "StructuralBasis",
    "build_candidate_profile",
    "capability_signals",
    "entry_role",
    "select_representative_entries",
]
