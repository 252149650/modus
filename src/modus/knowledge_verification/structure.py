"""Canonical path checks for the authored project knowledge tree."""

from __future__ import annotations

from pathlib import Path

from ..knowledge_identity import KnowledgePathIdentity
from ..paths import ModusPaths
from .models import VerificationFinding, finding


# 路径身份由公共分类器定义，校验器不维护另一套布局词汇。
# 旧 business 根给出具体迁移位置；未知路径保守报告而不移动文件。
def verify_canonical_paths(
    paths: ModusPaths, documents: tuple[Path, ...]
) -> list[VerificationFinding]:
    """Reject Markdown that sits outside every supported knowledge identity."""
    findings: list[VerificationFinding] = []
    for path in documents:
        relative = paths.relative(path)
        if KnowledgePathIdentity.classify(paths, relative) is not None:
            continue
        if relative.startswith("modus/knowledge/business/"):
            detail = "项目业务知识应位于 modus/knowledge/domain/<domain>/references/business/"
            code = "business-path-invalid"
        else:
            detail = "Markdown 位于规范项目知识布局之外"
            code = "knowledge-path-unsupported"
        findings.append(finding(relative, code, detail))
    return findings


__all__ = ["verify_canonical_paths"]
