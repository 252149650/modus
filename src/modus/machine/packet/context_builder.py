"""Intent-driven, budget-accounted authoring context assembly.

The builder selects summaries first, then follows only representative-path
handles for chosen capabilities. It never treats cursor exhaustion as a
completion condition and never serializes the underlying graph.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any, cast

from ..errors import BudgetError
from ..receipts import CoverageReceipt
from ..states import Completeness, ExecutionStatus
from ..store.contracts import (
    AnalysisBundle,
    EvidenceBudget,
    EvidencePage,
    EvidenceQuery,
    EvidenceRecord,
    SelectionView,
)
from .contracts import (
    AuthoringContextPacket,
    ContextRequest,
    ExpansionHandle,
    HandleKind,
    PacketBudget,
    PacketScope,
    TaskIntent,
)


# 按 UTF-8 序列化字节计费，中文字符数不能代替传输大小。
# 序列化配置与预算计费保持一致，缩进或转义策略变化会影响可交付大小。
def _size(packet: AuthoringContextPacket) -> int:
    return len(
        json.dumps(
            packet.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


# 创作意图映射到最小必要视图和记录类别。
# 服务边界读取服务概要，约束提取只读取规则信号。
# 请求范围原样传递，改变意图不应隐式扩大候选或路径。
# 不同意图复用同一查询协议，预算与范围约束不因视图切换而丢失。
def _intent_request(
    intent: TaskIntent,
    scope: PacketScope,
    budget: PacketBudget,
) -> EvidenceQuery:
    if intent is TaskIntent.SERVICE_BOUNDARY:
        view: SelectionView = "services"
        record_types: tuple[str, ...] = ("serviceBoundary", "servicesSummary")
    elif intent is TaskIntent.CONSTRAINT_EXTRACTION:
        view = "evidence"
        record_types = ("authoringSignal",)
    elif intent is TaskIntent.COVERAGE_AUDIT:
        view = "evidence"
        record_types = ()
    else:
        view = "scope"
        record_types = ("scopeSummary", "candidateSummary")
    return EvidenceQuery(
        view=view,
        candidate_ids=scope.candidate_ids,
        capability_ids=scope.capability_ids,
        boundary_ids=scope.boundary_ids,
        paths=scope.paths,
        symbols=scope.symbols,
        record_types=record_types,
        budget=EvidenceBudget(
            max_items=budget.max_items,
            max_bytes=budget.max_bytes,
        ),
    )


# 候选摘要保留结构依据、库存与降级信息，供作者判断是否成立。
# 规则信号稍后挂到所属候选，避免散落为无归属的全局结论。
# 列表与字典复制后组装，不修改查询返回的原始细节。
# 覆盖缺口数量和省略数量随摘要保留，首屏不是完整证据的暗示。
def _candidate_row(details: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposalId": str(details.get("candidateId") or ""),
        "disposition": str(details.get("disposition") or "remainder"),
        "partition": str(details.get("partition") or ""),
        "capabilityIds": list(details.get("capabilityIds") or []),
        "structuralBasis": dict(details.get("structuralBasis") or {}),
        "inventory": dict(details.get("inventory") or {}),
        "capabilitySignals": dict(details.get("capabilitySignals") or {}),
        "representativeEntries": list(details.get("representativeEntries") or []),
        "authoritativeObjects": list(details.get("authoritativeObjects") or []),
        "terminalEffects": list(details.get("terminalEffects") or []),
        "evidenceTiers": dict(details.get("evidenceTiers") or {}),
        "degradationReasons": list(details.get("degradationReasons") or []),
        "coverageReceipts": list(details.get("coverageReceipts") or []),
        "coverageGapCount": int(details.get("coverageGapCount") or 0),
        "omittedCounts": dict(details.get("omittedCounts") or {}),
        "authoringSignals": [],
    }


# 规则信号保留完整来源、精度和见证，不只返回匹配词。
# 候选关联让作者能识别信号究竟支撑哪项范围提议。
# 信号以原记录 ID 和见证交付，作者可继续展开核对来源。
def _authoring_signal_row(record: EvidenceRecord) -> dict[str, Any]:
    """Preserve provenance while keeping signals nested under their proposal."""

    payload = record.to_dict()
    return {
        "recordId": payload["recordId"],
        "kind": payload["kind"],
        "candidateIds": list(cast(list[str], payload["candidateIds"])),
        "resolution": payload["resolution"],
        "sourceLayer": payload["sourceLayer"],
        "witness": dict(cast(dict[str, Any], payload["witness"])),
        "details": dict(cast(dict[str, Any], payload["details"])),
    }


# 扩展句柄按种类和引用去重，提示文字变化不生成重复入口。
def _append_handle(
    handles: list[ExpansionHandle],
    kind: HandleKind,
    reference: str,
    hint: str,
) -> None:
    if reference and not any(
        handle.kind == kind and handle.reference == reference for handle in handles
    ):
        handles.append(ExpansionHandle(kind, reference, hint))


# 封套始终携带原请求范围与分析身份，便于跨进程核验。
# 内部可变列表在公开边界转为元组，避免组装后被意外追加。
# 遗漏计数排序输出，保持相同预算下的结果稳定。
def _packet(
    *,
    bundle: AnalysisBundle,
    intent: TaskIntent,
    requested_scope: PacketScope,
    proposals: list[dict[str, Any]],
    capabilities: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    authorities: list[dict[str, Any]],
    effects: list[dict[str, Any]],
    ambiguities: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    handles: list[ExpansionHandle],
    receipt: CoverageReceipt,
    omitted: Counter[str],
) -> AuthoringContextPacket:
    return AuthoringContextPacket(
        intent=intent,
        bundle_digest=bundle.bundle_digest,
        baseline_digest=bundle.baseline_digest,
        requested_scope=requested_scope,
        proposals=tuple(proposals),
        capabilities=tuple(capabilities),
        representative_paths=tuple(paths),
        authoritative_objects=tuple(authorities),
        terminal_effects=tuple(effects),
        ambiguities=tuple(ambiguities),
        coverage_receipts=(receipt,),
        source_anchors=tuple(anchors),
        expansion_handles=tuple(handles),
        omitted_counts=dict(sorted(omitted.items())),
    )


# 最终预算覆盖所有直接决策项及嵌套规则信号。
# 先移除低优先级细节，再缩减概要，保留可继续展开的核心材料。
# 裁剪在副本中进行，初始包可用于诊断本次遗漏。
# 每次裁剪更新遗漏计数，不能以静默删除换取体积达标。
# 固定封套也放不下时抛出预算错误，不交付不完整协议。
# 最终序列化后再次核算，集合项数限制不能代替字节限制。
def _trim_to_budget(
    packet: AuthoringContextPacket,
    budget: PacketBudget,
    omitted: Counter[str],
) -> AuthoringContextPacket:
    """Drop lowest-priority rows until the final serialized packet fits."""
    values: dict[str, list[Any]] = {
        "ambiguities": list(packet.ambiguities),
        "representative_paths": list(packet.representative_paths),
        "terminal_effects": list(packet.terminal_effects),
        "authoritative_objects": list(packet.authoritative_objects),
        "capabilities": list(packet.capabilities),
        "proposals": [
            {
                **proposal,
                "authoringSignals": list(proposal.get("authoringSignals") or []),
            }
            for proposal in packet.proposals
        ],
        "source_anchors": list(packet.source_anchors),
        "expansion_handles": list(packet.expansion_handles),
    }
    result = packet

    # 嵌套信号也消耗条目预算，不能通过嵌套结构绕过 max_items。
    def content_items() -> int:
        direct_items = sum(
            len(values[name])
            for name in (
                "proposals",
                "capabilities",
                "representative_paths",
                "authoritative_objects",
                "terminal_effects",
                "ambiguities",
            )
        )
        nested_signals = sum(
            len(proposal.get("authoringSignals") or [])
            for proposal in values["proposals"]
            if isinstance(proposal, dict)
        )
        return direct_items + nested_signals

    while _size(result) > budget.max_bytes or content_items() > budget.max_items:
        removed = False
        for proposal in reversed(values["proposals"]):
            signals = proposal.get("authoringSignals") if isinstance(proposal, dict) else None
            if isinstance(signals, list) and signals:
                signals.pop()
                omitted["authoringSignals"] += 1
                removed = True
                break
        if removed:
            result = replace(
                packet,
                proposals=tuple(values["proposals"]),
                capabilities=tuple(values["capabilities"]),
                representative_paths=tuple(values["representative_paths"]),
                authoritative_objects=tuple(values["authoritative_objects"]),
                terminal_effects=tuple(values["terminal_effects"]),
                ambiguities=tuple(values["ambiguities"]),
                source_anchors=tuple(values["source_anchors"]),
                expansion_handles=tuple(values["expansion_handles"]),
                omitted_counts=dict(sorted(omitted.items())),
            )
            continue
        for name in (
            "ambiguities",
            "representative_paths",
            "terminal_effects",
            "authoritative_objects",
            "capabilities",
            "proposals",
            "source_anchors",
            "expansion_handles",
        ):
            if values[name]:
                values[name].pop()
                omitted[name] += 1
                removed = True
                break
        if not removed:
            break
        result = replace(
            packet,
            proposals=tuple(values["proposals"]),
            capabilities=tuple(values["capabilities"]),
            representative_paths=tuple(values["representative_paths"]),
            authoritative_objects=tuple(values["authoritative_objects"]),
            terminal_effects=tuple(values["terminal_effects"]),
            ambiguities=tuple(values["ambiguities"]),
            source_anchors=tuple(values["source_anchors"]),
            expansion_handles=tuple(values["expansion_handles"]),
            omitted_counts=dict(sorted(omitted.items())),
        )
    # 任何预算遗漏都把交付收据降为部分完整。
    # 原始观察计数继续保留，遗漏详情由独立计数说明。
    if omitted and result.coverage_receipts:
        original = result.coverage_receipts[0]
        receipt = CoverageReceipt(
            original.producer,
            original.scope_kind,
            original.scope_id,
            original.status,
            Completeness.PARTIAL,
            "machine.packet.budget-exhausted",
            dict(original.counts),
        )
        result = replace(result, coverage_receipts=(receipt,))
    if _size(result) > budget.max_bytes:
        raise BudgetError("machine.packet.fixed-envelope-exceeds-budget")
    return result


# 先选择摘要，再沿选中候选读取有限代表路径和规则信号。
# 不自动追完分页，避免创作上下文退化成全仓库转储。
# 低置信度候选有独立配额，既保留缺口又不淹没主要提议。
# 来源锚点与扩展句柄提供后续核验入口，不替代作者判断。
# 完整度描述本次上下文选择，不代表业务范围已确认。
def package_context(
    bundle: AnalysisBundle,
    request: ContextRequest,
) -> AuthoringContextPacket:
    """Build one task-scoped packet without exhausting any continuation cursor."""
    from .query import query_evidence

    selected_intent = request.intent
    selected_scope = request.scope
    selected_budget = request.budget
    signal_selection: EvidencePage | None = None
    scope_for_selection = selected_scope
    if (
        selected_intent is TaskIntent.MODULE_AUTHORING
        and not selected_scope.candidate_ids
        and (selected_scope.paths or selected_scope.symbols)
    ):
        signal_selection = query_evidence(
            bundle,
            EvidenceQuery(
                view="evidence",
                capability_ids=selected_scope.capability_ids,
                paths=selected_scope.paths,
                symbols=selected_scope.symbols,
                record_types=("authoringSignal",),
                budget=EvidenceBudget(
                    max_items=selected_budget.max_items,
                    max_bytes=selected_budget.max_bytes,
                ),
            ),
        )
        # 按路径或符号创作时，从实际命中的信号反推候选。
        # 没有候选关联不能回退为全范围摘要，避免显式范围失效。
        derived_candidates = tuple(
            sorted(
                {
                    candidate_id
                    for item in signal_selection.items
                    for candidate_id in item.candidate_ids
                }
            )
        )
        scope_for_selection = replace(selected_scope, candidate_ids=derived_candidates)
    query = _intent_request(selected_intent, scope_for_selection, selected_budget)
    # 原请求含定位条件却未找到候选时返回空选择。
    # 空结果比未经授权扩大到全部候选更符合查询契约。
    selection = (
        query_evidence(bundle, query)
        if scope_for_selection.candidate_ids or not (selected_scope.paths or selected_scope.symbols)
        else EvidencePage(bundle.bundle_digest, bundle.baseline_digest, "scope", ())
    )
    proposals: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []
    representative_paths: list[dict[str, Any]] = []
    authorities: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    handles: list[ExpansionHandle] = []
    omitted: Counter[str] = Counter()
    low_resolution = 0
    scope_summary: dict[str, Any] = {}
    selected_candidate_ids: list[str] = []
    for item in selection.items:
        details = dict(item.details)
        record_type = str(details.get("recordType") or "")
        if record_type == "scopeSummary":
            scope_summary = details
            continue
        if record_type == "candidateSummary":
            disposition = str(details.get("disposition") or "remainder")
            is_low = disposition != "proposed" or bool(details.get("coverageGapCount"))
            # 低精度与不完整候选共享有限配额，超额部分计入遗漏。
            # 被保留的低精度候选仍附歧义说明，不能混同于明确提议。
            if is_low and low_resolution >= selected_budget.low_resolution_quota:
                omitted["lowResolutionProposals"] += 1
                continue
            low_resolution += int(is_low)
            row = _candidate_row(details)
            proposals.append(row)
            candidate_id = str(details.get("candidateId") or "")
            if candidate_id:
                selected_candidate_ids.append(candidate_id)
                handles.append(
                    ExpansionHandle(
                        HandleKind.CANDIDATE,
                        candidate_id,
                        "Expand only when this proposal affects the authoring decision.",
                    )
                )
            # 能力先以摘要句柄交付，只有影响当前决策时才继续展开。
            # 证据层级与降级原因随能力保留，不能由所属候选隐去。
            for capability_id in row["capabilityIds"]:
                capabilities.append(
                    {
                        "capabilityId": capability_id,
                        "proposalId": candidate_id,
                        "evidenceTiers": dict(row["evidenceTiers"]),
                        "degradationReasons": list(row["degradationReasons"]),
                    }
                )
                handles.append(
                    ExpansionHandle(HandleKind.CAPABILITY, capability_id, "Inspect its paths.")
                )
            authorities.extend(
                {"proposalId": candidate_id, "identity": value}
                for value in row["authoritativeObjects"]
            )
            effects.extend(
                {"proposalId": candidate_id, **dict(value)}
                for value in row["terminalEffects"]
                if isinstance(value, dict)
            )
            # 代表入口提供可定位源码锚点，重复位置只保存一次。
            # 这些锚点来自摘要，不要求读取全部成员文件。
            for entry in row["representativeEntries"]:
                if not isinstance(entry, dict):
                    continue
                anchor = {
                    key: entry.get(key)
                    for key in ("path", "startLine", "endLine", "sourceDigest", "symbol")
                }
                if anchor.get("path") and anchor not in anchors:
                    anchors.append(anchor)
            if is_low:
                ambiguities.append(
                    {
                        "proposalId": candidate_id,
                        "marker": "low-confidence-or-incomplete",
                        "disposition": disposition,
                        "coverageGapCount": row["coverageGapCount"],
                    }
                )
        elif record_type in {"serviceBoundary", "servicesSummary"}:
            proposals.append(details)
        elif record_type == "authoringSignal":
            kind = item.record_id.split(".", 1)[0]
            row = {**details, "witness": item.witness.to_dict() if item.witness else None}
            if kind == "effect":
                effects.append(row)
            elif kind == "resource":
                authorities.append(row)
            else:
                proposals.append(row)
    if selected_intent is TaskIntent.MODULE_AUTHORING:
        signal_scope = tuple(selected_candidate_ids) or scope_for_selection.candidate_ids
        if signal_selection is None:
            signal_selection = query_evidence(
                bundle,
                EvidenceQuery(
                    view="evidence",
                    candidate_ids=signal_scope,
                    capability_ids=selected_scope.capability_ids,
                    paths=selected_scope.paths,
                    symbols=selected_scope.symbols,
                    record_types=("authoringSignal",),
                    budget=EvidenceBudget(
                        max_items=selected_budget.max_items,
                        max_bytes=selected_budget.max_bytes,
                    ),
                ),
            )
        # 模块规则只挂到已选提议，避免把其他候选材料混入正文依据。
        # 找不到所属提议的信号仍保留为歧义，不能静默丢失。
        proposals_by_id = {
            str(proposal.get("proposalId") or ""): proposal
            for proposal in proposals
            if proposal.get("proposalId")
        }
        for item in signal_selection.items:
            row = _authoring_signal_row(item)
            matched = False
            for candidate_id in item.candidate_ids:
                proposal = proposals_by_id.get(candidate_id)
                if proposal is None:
                    continue
                signals = proposal.setdefault("authoringSignals", [])
                if isinstance(signals, list):
                    signals.append(row)
                    matched = True
            witness = item.witness
            if witness is not None:
                anchor = witness.to_dict()
                if anchor not in anchors:
                    anchors.append(anchor)
                _append_handle(
                    handles,
                    HandleKind.PATH,
                    witness.path,
                    "Read current source when this signal can change the authored conclusion.",
                )
                if witness.symbol:
                    _append_handle(
                        handles,
                        HandleKind.SYMBOL,
                        witness.symbol,
                        "Inspect the exact symbol rather than treating its file as one capability.",
                    )
            if not matched:
                ambiguities.append(
                    {
                        "marker": "unbound-authoring-signal",
                        "candidateIds": list(item.candidate_ids),
                        "signal": row,
                    }
                )
        # 规则读取缺口与正常规则分别表达。
        # 尚有后续页面只记录遗漏，不在本次请求中自动穷尽。
        for gap in signal_selection.coverage_gaps:
            ambiguities.append(
                {
                    "marker": "authoring-signal-gap",
                    "gap": gap.to_dict(),
                }
            )
        if signal_selection.next_cursor:
            omitted["authoringSignalPages"] += 1
    if selected_candidate_ids and selected_intent in {
        TaskIntent.SCOPE_DISCOVERY,
        TaskIntent.MODULE_AUTHORING,
        TaskIntent.REINIT_DIFF,
    }:
        path_selection = query_evidence(
            bundle,
            EvidenceQuery(
                view="scope",
                candidate_ids=tuple(selected_candidate_ids),
                capability_ids=selected_scope.capability_ids,
                record_types=("navigationStep",),
                budget=EvidenceBudget(
                    max_items=min(selected_budget.max_items, len(selected_candidate_ids) * 8),
                    max_bytes=selected_budget.max_bytes,
                ),
            ),
        )
        # 代表路径按能力分别限额，防止高连接能力占满上下文。
        # 预算未返回的路径用计数表达，不能当作图中不存在。
        per_capability: defaultdict[str, int] = defaultdict(int)
        for item in path_selection.items:
            row = dict(item.details)
            capability_id = str(row.get("capabilityId") or "")
            if per_capability[capability_id] >= selected_budget.max_paths_per_capability:
                omitted["representativePaths"] += 1
                continue
            per_capability[capability_id] += 1
            representative_paths.append(row)
        if path_selection.next_cursor:
            omitted["pathSelectionPages"] += 1
    # 全局概要用于估算未交付候选，选择页游标另行记录。
    # 材料缺失与预算裁剪都影响完整度，但保留不同原因。
    total_candidates = int(scope_summary.get("candidateCount") or len(proposals))
    if total_candidates > len(proposals):
        omitted["proposals"] += total_candidates - len(proposals)
    if selection.next_cursor:
        omitted["selectionPages"] += 1
    completeness = Completeness.PARTIAL if omitted or ambiguities else Completeness.COMPLETE
    if not selection.items and not selection.coverage_gaps:
        completeness = Completeness.UNAVAILABLE
    reason = (
        "machine.packet.no-matching-evidence"
        if completeness is Completeness.UNAVAILABLE
        else "machine.packet.budget-exhausted"
        if omitted
        else "machine.packet.low-confidence-evidence"
        if ambiguities
        else ""
    )
    # 收据统计本次实际组装内容，方便消费者评估上下文规模。
    # 最后仍需按最终封套字节数裁剪，不能只依赖子查询各自预算。
    receipt = CoverageReceipt(
        "context-builder",
        "intent",
        str(selected_intent),
        ExecutionStatus.ANALYZED,
        completeness,
        reason,
        {
            "proposals": len(proposals),
            "capabilities": len(capabilities),
            "representativePaths": len(representative_paths),
            "authoringSignals": sum(
                len(proposal.get("authoringSignals") or [])
                for proposal in proposals
                if isinstance(proposal, dict)
            ),
        },
    )
    packet = _packet(
        bundle=bundle,
        intent=selected_intent,
        requested_scope=selected_scope,
        proposals=proposals,
        capabilities=capabilities,
        paths=representative_paths,
        authorities=authorities,
        effects=effects,
        ambiguities=ambiguities,
        anchors=anchors,
        handles=handles,
        receipt=receipt,
        omitted=omitted,
    )
    # 多个子查询组合后可能超出总预算，统一在交付边界收敛。
    # 返回的原始 requested_scope 不随裁剪改变。
    return _trim_to_budget(packet, selected_budget, omitted)


__all__ = ["package_context"]
