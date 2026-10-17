"""Infer Domain and Module scope from ranked narrative knowledge."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from .stages.lexical import tokenize

RouteKind = Literal["domain", "module"]


# 多个正文片段共享一个领域或模块身份，不能重复占据路由名额。
# 路由保存支撑片段 ID，推断结果可回到原文核验。
@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """Deduplicated routing identity assembled from one or more sections."""

    kind: RouteKind
    identity: str
    domain: str
    module: str | None
    path: str
    evidence_id: str
    text: str
    terms: frozenset[str]

    def as_record(self) -> dict[str, Any]:
        """Return the historical mapping shape consumed by ranking code."""

        return {
            "kind": self.kind,
            "identity": self.identity,
            "domain": self.domain,
            "module": self.module,
            "path": self.path,
            "evidenceId": self.evidence_id,
            "text": self.text,
            "terms": set(self.terms),
        }


# 退役条目不参与路由，避免把用户带到不再维护的主定义。
# 元数据与高层正文共同提供线索，细节章节不主导模块身份。
# 正文摘要限长，单个大文档不能耗尽路由计算预算。
# 目录按身份排序，片段按位置排序，消除扫描顺序的影响。
def build_route_catalog(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one stable routing record per Domain and Module identity."""

    grouped = _group_route_sections(index)

    catalog: list[dict[str, Any]] = []
    for (kind, identity), sections in sorted(grouped.items()):
        sections.sort(key=lambda item: (item["path"], item["lineStart"]))
        first = sections[0]
        frontmatter = first.get("frontmatter") or {}
        declared = " ".join(
            str(frontmatter.get(key) or "")
            for key in (
                "moduleId",
                "parent",
                "name",
                "displayName",
                "aliases",
                "description",
                "responsibilities",
            )
        )
        prose = "\n".join(
            str(item.get("text") or "")
            for item in sections
            if int(item.get("headingLevel") or 6) <= 2 and not item.get("facet")
        )[:2400]
        text = "\n".join([identity, first["path"], first.get("heading") or "", declared, prose])
        scope = first.get("scope") or {}
        catalog.append(
            RouteCandidate(
                kind=kind,
                identity=identity,
                domain=str(scope.get("domain") or ""),
                module=str(scope.get("module") or "") or None,
                path=str(first["path"]),
                evidence_id=str(first["id"]),
                text=text,
                terms=frozenset(tokenize(text)),
            ).as_record()
        )
    return catalog


def _group_route_sections(
    index: dict[str, Any],
) -> dict[tuple[RouteKind, str], list[dict[str, Any]]]:
    """领域和模块各自使用稳定身份；退役片段不参与路由目录。"""
    grouped: dict[tuple[RouteKind, str], list[dict[str, Any]]] = defaultdict(list)
    for item in index.get("sections") or []:
        if item.get("status") == "deprecated":
            continue
        scope = item.get("scope") or {}
        domain = scope.get("domain")
        module = scope.get("module")
        if item.get("sourceType") == "project-domain" and domain:
            grouped[("domain", str(domain))].append(item)
        elif item.get("sourceType") == "project-module" and domain and module:
            grouped[("module", f"{domain}/{module}")].append(item)
    return grouped


# 路由分数结合稀有词命中和已排序正文证据。
# 分数只作为范围提示，不证明业务归属或事实正确。
# 保留次强模块有助于跨领域召回，但最终仍受正文预算约束。
# 无有效候选时返回原路由，不能凭空生成默认领域。
# 推断结果复制原对象，调用方的显式请求信息仍可保留。
def infer_knowledge_route(
    index: dict[str, Any],
    candidates: list[dict[str, Any]],
    query_text: str,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of ``route`` enriched with evidence-backed scope hints."""

    catalog = build_route_catalog(index)
    if not catalog:
        return route

    query_terms = set(tokenize(query_text))
    frequencies: Counter[str] = Counter()
    for record in catalog:
        frequencies.update(record["terms"])

    candidate_scores: defaultdict[str, float] = defaultdict(float)
    for item in candidates:
        scope = item.get("scope") or {}
        domain = scope.get("domain")
        module = scope.get("module")
        if domain:
            key = f"domain:{domain}"
            candidate_scores[key] = max(candidate_scores[key], float(item.get("score") or 0.0))
        if domain and module:
            key = f"module:{domain}/{module}"
            candidate_scores[key] = max(candidate_scores[key], float(item.get("score") or 0.0))

    scores: dict[tuple[str, str], tuple[float, list[str], dict[str, Any]]] = {}
    total = max(1, len(catalog))
    for record in catalog:
        matched = sorted(query_terms & record["terms"], key=lambda value: (-len(value), value))
        score = sum(
            math.log(1.0 + (total + 1) / (frequencies[term] + 1)) * (1.6 if len(term) > 2 else 1.0)
            for term in matched
        )
        score += 20.0 * candidate_scores[f"{record['kind']}:{record['identity']}"]
        scores[(record["kind"], record["identity"])] = (score, matched, record)

    module_candidates = sorted(
        (value for key, value in scores.items() if key[0] == "module" and value[0] > 0),
        key=lambda value: (-value[0], value[2]["identity"]),
    )
    # Inferred scope reorders evidence; it never excludes it. Keeping a related
    # secondary module therefore improves cross-domain recall without widening
    # the final body budget.
    module_threshold = module_candidates[0][0] * 0.35 if module_candidates else 0.0
    ranked_modules = [value for value in module_candidates if value[0] >= module_threshold][:5]
    ranked_domains = _rank_domains(scores, ranked_modules)
    if not ranked_domains and not ranked_modules:
        return route

    inferred = dict(route)
    inferred["domains"] = [value[2]["identity"] for value in ranked_domains]
    inferred["modules"] = [value[2]["identity"] for value in ranked_modules]
    inferred["inferred"] = True
    details = [
        {
            "kind": value[2]["kind"],
            "id": value[2]["identity"],
            "path": value[2]["path"],
            "evidenceId": value[2]["evidenceId"],
            "matchedTerms": value[1][:8],
            "score": round(value[0], 6),
        }
        for value in [*ranked_domains, *ranked_modules]
    ]
    previous = str(route.get("rationale") or "").strip()
    inferred["rationale"] = "; ".join(
        part
        for part in (
            previous,
            "知识路由由去重后的领域/模块记录推断",
        )
        if part
    )
    inferred["rationaleDetails"] = details
    strongest = max((item["score"] for item in details), default=0.0)
    inferred["confidence"] = round(strongest / (strongest + 3.0), 4)
    return inferred


# 通过 evidenceId 返回支撑路由的原始章节，保持推断可解释。
# 附加分数信号写在副本上，不污染共享索引记录。
def route_records(
    index: dict[str, Any], route: dict[str, Any], *, kind: str
) -> list[dict[str, Any]]:
    """Resolve route rationale back to its source section records."""

    by_id = {item["id"]: item for item in index.get("sections") or []}
    details = [item for item in route.get("rationaleDetails") or [] if item.get("kind") == kind]
    result: list[dict[str, Any]] = []
    for detail in details:
        record = by_id.get(detail.get("evidenceId"))
        if record is not None:
            copied = dict(record)
            copied["scoreSignals"] = {"knowledgeRoute": 1.0}
            result.append(copied)
    return result


# 子模块的强命中可以支持父领域，但按折扣传播。
# 优先保留已选模块的父领域，使层级导航保持连贯。
# 非常强的直接领域命中仍可补入，避免模块召回遮蔽独立领域。
# 最终领域数量有界，不能把所有弱匹配都升级为当前范围。
def _rank_domains(
    scores: dict[tuple[str, str], tuple[float, list[str], dict[str, Any]]],
    ranked_modules: list[tuple[float, list[str], dict[str, Any]]],
) -> list[tuple[float, list[str], dict[str, Any]]]:
    domain_values: list[tuple[float, list[str], dict[str, Any]]] = []
    for key, value in scores.items():
        if key[0] != "domain":
            continue
        child_score = max(
            (
                module_value[0]
                for module_key, module_value in scores.items()
                if module_key[0] == "module" and module_value[2]["domain"] == value[2]["identity"]
            ),
            default=0.0,
        )
        domain_values.append((max(value[0], child_score * 0.9), value[1], value[2]))

    scored = sorted(
        (value for value in domain_values if value[0] > 0),
        key=lambda value: (-value[0], value[2]["identity"]),
    )
    if not ranked_modules:
        return scored[:3]

    by_domain = {value[2]["identity"]: value for value in scored}
    ranked: list[tuple[float, list[str], dict[str, Any]]] = []
    for module_value in ranked_modules:
        parent = module_value[2]["domain"]
        if parent in by_domain and by_domain[parent] not in ranked:
            ranked.append(by_domain[parent])
    strong_direct = ranked_modules[0][0] * 1.25
    for domain_value in scored:
        if domain_value[0] >= strong_direct and domain_value not in ranked:
            ranked.append(domain_value)
        if len(ranked) >= 3:
            break
    return ranked[:3]


__all__ = ["RouteCandidate", "build_route_catalog", "infer_knowledge_route", "route_records"]
