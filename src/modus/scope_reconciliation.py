"""Deterministic source comparison, not automatic business-boundary approval."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .paths import MANAGED_ROOT_FILES, PLATFORM_OUTPUT_DIRS
from .scope_models import ScopeRef
from .scope_sources import (
    MAX_RECORDS,
    ScopeSourceError,
    load_input,
    load_material,
    normalized_label,
    read_material_bytes,
    validate_payload,
)


def _scope(value: str) -> ScopeRef:
    try:
        return ScopeRef.parse(value)
    except ValueError as exc:
        raise ScopeSourceError("候选包含不合法的ScopeRef") from exc


def _inventory_digest(sources: list[dict[str, Any]]) -> str:
    payload = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_scope_sources(
    root: str, input_file: str, *, allowed_roots: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Read only explicit input; no discovery, graph building, or card writes."""
    base = Path(root).absolute()
    boundaries = tuple(Path(p).absolute() for p in allowed_roots)
    data, input_digest = load_input(base, input_file, boundaries)
    materials = [load_material(base, spec, boundaries) for spec in data["sources"]]
    if sum(len(m.records) for m in materials) > MAX_RECORDS:
        raise ScopeSourceError("全部来源记录合计超过上限")

    candidates: dict[str, dict[str, Any]] = {}
    group_refs: defaultdict[str, set[str]] = defaultdict(set)
    names: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    folded_refs: defaultdict[str, set[str]] = defaultdict(set)
    for material in materials:
        for record in material.records:
            if record.scope_ref:
                ref = _scope(record.scope_ref)
                candidates.setdefault(
                    str(ref), {"scopeRef": str(ref), "labels": [], "origins": [], "locations": []}
                )
                item = candidates[str(ref)]
                if record.label not in item["labels"]:
                    item["labels"].append(record.label)
                item["origins"].append(
                    {"sourceId": material.source_id, "recordId": record.record_id}
                )
                if ref.kind == "domain":
                    group_refs[normalized_label(record.label)].add(ref.domain)
                    group_refs[ref.domain].add(ref.domain)

    evidence_bytes = 0
    evidence_files: dict[str, tuple[int, str]] = {}
    evidence_errors: dict[str, str] = {}
    excluded_roots = {
        name.casefold() for name in ("modus", ".modus", ".git", *PLATFORM_OUTPUT_DIRS)
    }
    excluded_files = {name.casefold() for name in MANAGED_ROOT_FILES}
    extra_seen: set[str] = set()
    for declared in data.get("candidates", []):
        ref = _scope(declared["scopeRef"])
        if str(ref) in extra_seen:
            raise ScopeSourceError("候选ScopeRef重复")
        extra_seen.add(str(ref))
        item = candidates.setdefault(
            str(ref), {"scopeRef": str(ref), "labels": [], "origins": [], "locations": []}
        )
        if declared["label"] not in item["labels"]:
            item["labels"].append(declared["label"])
        item["reason"] = declared["reason"]
        if ref.kind == "domain":
            group_refs[normalized_label(declared["label"])].add(ref.domain)
        for location in declared.get("locations", []):
            checked = dict(location)
            try:
                path = str(Path(location["path"]))
                parts = Path(path).parts
                if (
                    Path(path).is_absolute()
                    or not parts
                    or parts[0].casefold() in excluded_roots
                    or path.casefold() in excluded_files
                ):
                    raise ScopeSourceError("源码证据必须位于项目内，不能使用生成知识或工具输出")
                if path in evidence_errors:
                    raise ScopeSourceError(evidence_errors[path])
                if path not in evidence_files:
                    if len(evidence_files) + len(evidence_errors) >= 100:
                        raise ScopeSourceError("源码文件数超过本轮对照预算")
                    if evidence_bytes + 4 * 1024 * 1024 > 32 * 1024 * 1024:
                        raise ScopeSourceError("源码读取超过本轮对照预算")
                    try:
                        raw = read_material_bytes(base, path)
                        evidence_bytes += len(raw)
                        line_count = len(raw.decode("utf-8").splitlines())
                    except (ScopeSourceError, UnicodeError) as exc:
                        reason = (
                            str(exc) if isinstance(exc, ScopeSourceError) else "源码不是UTF-8文本"
                        )
                        evidence_errors[path] = reason
                        raise ScopeSourceError(reason) from exc
                    evidence_files[path] = (line_count, hashlib.sha256(raw).hexdigest())
                line_count, digest = evidence_files[path]
                start, end = location["startLine"], location["endLine"]
                if not 1 <= start <= end <= line_count:
                    raise ScopeSourceError("源码定位范围不合法")
                checked.update(digest=digest, status="location-checked")
                # A valid range does not prove a qualified symbol, ownership or a call edge.
                checked["semanticStatus"] = "requires-source-review"
            except (ScopeSourceError, UnicodeError) as exc:
                checked.update(
                    status="unavailable",
                    reason=str(exc) if isinstance(exc, ScopeSourceError) else "源码不是UTF-8文本",
                )
            item["locations"].append(checked)

    for key, item in candidates.items():
        ref = _scope(key)
        group_refs[ref.domain].add(ref.domain)
        folded_refs[key.casefold()].add(key)
        for label in item["labels"]:
            names[(ref.domain, normalized_label(label))].add(key)
    collisions = [sorted(refs) for refs in folded_refs.values() if len(refs) > 1]
    # Domain casing also conflicts on case-insensitive filesystems even with different children.
    domains: defaultdict[str, set[str]] = defaultdict(set)
    for key in candidates:
        domain = _scope(key).domain
        domains[domain.casefold()].add(domain)
    domain_collisions = {d for values in domains.values() if len(values) > 1 for d in values}
    collisions.extend(
        sorted(str(ScopeRef.for_domain(d)) for d in values)
        for values in domains.values()
        if len(values) > 1
    )
    collisions = [list(group) for group in sorted({tuple(group) for group in collisions})]
    collision_refs = {ref for group in collisions for ref in group} | {
        key for key in candidates if _scope(key).domain in domain_collisions
    }

    records = {(m.source_id, r.record_id): r for m in materials for r in m.records}
    source_ids = {m.source_id for m in materials}
    unavailable_ids = {m.source_id for m in materials if m.error}
    unavailable_tree = any(
        spec["format"] == "mindmap" and spec["id"] in unavailable_ids for spec in data["sources"]
    )
    mappings: dict[tuple[str, str], dict[str, Any]] = {}
    mapping_keys: set[tuple[str, str]] = set()
    deferred_keys: set[tuple[str, str]] = set()
    deferred_mappings: list[dict[str, Any]] = []
    for mapping in data.get("mappings", []):
        mapping_key = mapping["sourceId"], mapping["recordId"]
        if mapping["sourceId"] not in source_ids or mapping_key in mapping_keys:
            raise ScopeSourceError("显式映射引用未知来源或重复记录")
        mapping_keys.add(mapping_key)
        if mapping_key not in records and mapping["sourceId"] not in unavailable_ids:
            raise ScopeSourceError("显式映射引用未知记录")
        if not mapping["scopeRefs"] or not mapping["reason"].strip():
            raise ScopeSourceError("显式映射需要目标及理由")
        targets = {str(_scope(ref)) for ref in mapping["scopeRefs"]}
        if targets - candidates.keys() and not unavailable_tree:
            raise ScopeSourceError("映射目标未在候选或分类树声明")
        if mapping["sourceId"] in unavailable_ids or targets - candidates.keys():
            deferred_keys.add(mapping_key)
            deferred_mappings.append(
                {
                    "mapping": mapping,
                    "reason": "来源不可用，记录或目标身份无法核实；恢复来源后重新对照",
                }
            )
            continue
        mappings[mapping_key] = mapping

    rows: list[dict[str, Any]] = []
    for (source_id, record_id), record in records.items():
        explicit = mappings.get((source_id, record_id))
        refs: set[str] = set()
        method = "unmatched"
        reason = "没有唯一对应；需结合职责和源码判断，不据此认定能力废弃"
        if (source_id, record_id) in deferred_keys:
            method, reason = (
                "mapping-deferred",
                "映射依赖不可用来源；恢复来源后重新核对，不回退到名称匹配",
            )
        elif explicit:
            refs = set(explicit["scopeRefs"])
            method, reason = "explicit-mapping", explicit["reason"]
        elif record.scope_ref:
            refs = {record.scope_ref}
            method, reason = "declared-identity", "沿用来源身份作为建议，不代表本轮范围已确认"
        elif record.level == "scenario":
            method, reason = (
                "scenario",
                "保留子项身份和父链；默认作为场景候选，独立模块需显式映射和责任证据",
            )
        else:
            for domain in group_refs.get(normalized_label(record.group), ()):
                matches = names.get((domain, normalized_label(record.label)), set())
                refs.update(ref for ref in matches if _scope(ref).kind == "module")
            if refs:
                method, reason = (
                    "exact-label-or-id",
                    "仅归一空白并在所属大类中匹配；语义与源码责任仍待回读",
                )
        status = "suggested" if len(refs) == 1 or explicit else "pending"
        if refs & collision_refs or (len(refs) > 1 and not explicit):
            status, reason = "conflict", "存在同名多义或大小写落盘冲突，不能自动合并"
        change = "review"
        if explicit:
            if len(refs) > 1:
                change = "split-candidate"
            elif record.scope_ref and record.scope_ref not in refs:
                change = "move-or-merge-candidate"
            else:
                change = "map-candidate"
        elif record.scope_ref:
            change = "preserve-candidate"
        rows.append(
            {
                "sourceId": source_id,
                **asdict(record),
                "candidateScopeRefs": sorted(refs),
                "match": method,
                "status": status,
                "change": change,
                "reason": reason,
            }
        )

    source_rows = [
        {
            "id": m.source_id,
            "status": "unavailable" if m.error else "available",
            "recordIds": [r.record_id for r in m.records],
            **({"digest": m.digest} if m.digest else {}),
            **({"reason": m.error} if m.error else {}),
        }
        for m in materials
    ]
    review = {
        "schemaVersion": 1,
        "inputDigest": input_digest,
        "inventoryDigest": _inventory_digest(source_rows),
        "sources": source_rows,
        "items": [
            {
                "sourceId": row["sourceId"],
                "recordId": row["record_id"],
                "scopeRefs": [],
                "disposition": "pending",
                "reason": row["reason"],
            }
            for row in rows
        ],
    }
    validate_payload(review, "review")
    unavailable = sum(m.error is not None for m in materials)
    unavailable_locations = sum(
        location["status"] == "unavailable"
        for candidate in candidates.values()
        for location in candidate["locations"]
    )
    return {
        "schemaVersion": 1,
        "status": "partial" if unavailable or unavailable_locations else "ok",
        "readOnly": True,
        "inputDigest": input_digest,
        "sources": [
            {**row, "provenance": m.provenance}
            for row, m in zip(source_rows, materials, strict=True)
        ],
        "summary": {
            "sources": len(materials),
            "unavailableSources": unavailable,
            "unavailableLocations": unavailable_locations,
            "records": len(rows),
            "identityConflicts": len(collisions),
            "deferredMappings": len(deferred_mappings),
            **dict(Counter(row["status"] for row in rows)),
        },
        "candidates": list(candidates.values()),
        "records": rows,
        "deferredMappings": deferred_mappings,
        "identityConflicts": collisions,
        "sourceReview": review,
        "limitations": [
            "结果仅为归属建议，所有来源处置仍待本轮范围确认",
            "源码定位可读不等于符号、调用关系或业务责任已获证明",
            "未运行图分析或语义聚类；相似名称不能自动归并",
        ],
    }


def validate_source_review(review: object, domains: dict[str, object]) -> list[str]:
    """Validate optional attribution in the sole existing authoring card."""
    try:
        validate_payload(review, "review")
        assert isinstance(review, dict)
        if review["inventoryDigest"] != _inventory_digest(review["sources"]):
            raise ScopeSourceError("来源清单与导入摘要不一致，需重新对照并审阅来源变化")
        expected: set[tuple[str, str]] = set()
        source_ids: set[str] = set()
        for source in review["sources"]:
            if source["id"] in source_ids:
                raise ScopeSourceError("来源ID重复")
            source_ids.add(source["id"])
            if source["status"] != "available":
                raise ScopeSourceError("存在不可用来源，不能声明来源融合完成")
            expected.update((source["id"], record_id) for record_id in source["recordIds"])
        seen: set[tuple[str, str]] = set()
        for item in review["items"]:
            key = item["sourceId"], item["recordId"]
            if key in seen or key not in expected:
                raise ScopeSourceError("来源处置重复或不属于所记录的来源快照")
            seen.add(key)
            if not item["reason"].strip() or item["disposition"] == "pending":
                raise ScopeSourceError("来源处置尚未完成或缺少理由")
            refs = item["scopeRefs"]
            if item["disposition"] == "formal" and not refs:
                raise ScopeSourceError("正式归属需要ScopeRef")
            if item["disposition"] != "formal" and refs:
                raise ScopeSourceError("非正式归属应以理由解释，不伪造正式Scope")
            for value in refs:
                ref = _scope(value)
                plan = domains.get(ref.domain)
                if not isinstance(plan, dict):
                    raise ScopeSourceError("来源归属不在本轮确认域中")
                if ref.kind == "module":
                    modules = plan.get("modules")
                    if (
                        not isinstance(modules, dict)
                        or ref.module_id is None
                        or ref.module_id.module not in modules
                    ):
                        raise ScopeSourceError("来源归属不在本轮确认模块中")
        if expected != seen:
            raise ScopeSourceError("存在遗漏的来源记录")
    except (ScopeSourceError, AssertionError) as exc:
        return [str(exc) or "来源对照结构不合法"]
    return []
