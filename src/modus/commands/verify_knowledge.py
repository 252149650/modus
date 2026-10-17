"""Read-only integrity verification for authored project knowledge.

``clean`` means that the Markdown tree is mechanically safe to publish. It
deliberately says nothing about prose depth, heading choices, topic coverage,
or editorial quality.
"""

from __future__ import annotations

from typing import Any

from ..knowledge_verification.discovery import discover_knowledge
from ..knowledge_verification.markdown_integrity import (
    verify_links,
    verify_markdown_basics,
    verify_portability_and_sources,
)
from ..knowledge_verification.metadata import verify_module_metadata
from ..knowledge_verification.models import VerificationFinding, finding
from ..knowledge_verification.publication import verify_publication_readiness
from ..knowledge_verification.structure import verify_canonical_paths
from ..paths import ModusPaths
from .verify_anchors import run_verify_anchors

__all__ = ["run_verify_knowledge"]


def _sort_key(item: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(item.get("document", "")),
        int(item.get("line") or item.get("claimedLine") or 0),
        str(item.get("finding", "")),
        str(item.get("detail", "")),
    )


# 结构校验只判定可机械验证的损坏，不替作者评判业务表述的深度。
# 锚点检查固定 fix=False，发布就绪检查也不能隐式修订知识。
def run_verify_knowledge(
    root: str = ".", *, domain: str | None = None, publication: bool = False
) -> dict[str, Any]:
    """Verify Markdown, links, source references, safety, and Module identity."""

    paths = ModusPaths.for_root(root)
    scope_root = paths.knowledge_dir
    findings: list[VerificationFinding] = []
    if domain is not None:
        paths.domain_references(domain)  # validates the path segment
        scope_root = paths.domain(domain)
        if not scope_root.is_dir() or scope_root.is_symlink():
            findings.append(
                finding(
                    paths.relative(scope_root),
                    "domain-missing",
                    f"指定领域 {domain!r} 不存在或不是常规目录",
                )
            )

    inventory = discover_knowledge(paths, scope_root)
    findings.extend(inventory.findings)
    findings.extend(verify_canonical_paths(paths, inventory.documents))
    findings.extend(verify_markdown_basics(paths, inventory.documents))
    findings.extend(verify_links(paths, inventory.documents))
    findings.extend(verify_portability_and_sources(paths, inventory.documents))
    findings.extend(verify_module_metadata(paths, inventory.modules))

    content_signals: dict[str, int] | None = None
    if publication:
        publication_findings, content_signals = verify_publication_readiness(
            paths, inventory, domain=domain
        )
        findings.extend(publication_findings)

    serialized = [item.to_dict() for item in findings]
    anchor_result = run_verify_anchors(root, fix=False, domain=domain)
    serialized.extend({"severity": "error", **item} for item in anchor_result["findings"])
    serialized.sort(key=_sort_key)

    counts: dict[str, int] = {}
    for item in serialized:
        code = str(item["finding"])
        counts[code] = counts.get(code, 0) + 1
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "clean" if not serialized else "findings",
        "scope": {"domain": domain},
        "counts": dict(sorted(counts.items())),
        "findings": serialized,
        "checkedDocuments": len(inventory.documents),
    }
    if publication:
        result["assurance"] = "publication-readiness"
        result["contentSignals"] = content_signals or {}
    return result
