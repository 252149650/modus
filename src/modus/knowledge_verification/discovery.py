"""Safely discover the Markdown files inside one knowledge verification scope."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..paths import ModusPaths
from .models import VerificationFinding, finding


# 领域身份从规范物理路径取得，不相信正文中自声明的其他领域。
@dataclass(frozen=True, slots=True)
class ModuleDocument:
    """A Module document bound to the Domain encoded by its physical path."""

    path: Path
    domain: str


@dataclass(frozen=True, slots=True)
class KnowledgeInventory:
    """Complete output of a bounded, non-following knowledge-tree scan."""

    documents: tuple[Path, ...]
    modules: tuple[ModuleDocument, ...]
    findings: tuple[VerificationFinding, ...]


# 模块必须平铺在领域的 references/modules 下。
# 异常嵌套作为结构问题报告，不能静默识别成另一个模块。
def _module_documents(
    paths: ModusPaths, documents: list[Path]
) -> tuple[list[ModuleDocument], list[VerificationFinding]]:
    modules: list[ModuleDocument] = []
    findings: list[VerificationFinding] = []
    for path in documents:
        try:
            relative = path.relative_to(paths.domain_dir)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) < 3 or parts[1:3] != ("references", "modules"):
            continue
        if len(parts) != 4:
            findings.append(
                finding(
                    paths.relative(path),
                    "module-path-invalid",
                    "模块文档必须平铺在 references/modules 下",
                )
            )
            continue
        modules.append(ModuleDocument(path, parts[0]))
    return modules, findings


# 使用不跟随链接的 stat，目录扫描不会跨出知识树。
# 单目录不可读只产生该位置的发现，其他目录仍继续核验。
# 最终文档排序在上层统一进行，不依赖遍历栈的顺序。
def _scan_documents(paths: ModusPaths, root: Path) -> tuple[list[Path], list[VerificationFinding]]:
    """Collect regular Markdown files while recording unreadable or linked entries."""
    documents: list[Path] = []
    findings: list[VerificationFinding] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            findings.append(
                finding(
                    paths.relative(directory),
                    "knowledge-path-unreadable",
                    str(exc),
                )
            )
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                findings.append(
                    finding(
                        paths.relative(path),
                        "knowledge-path-unreadable",
                        str(exc),
                    )
                )
                continue
            if stat.S_ISLNK(mode):
                findings.append(
                    finding(
                        paths.relative(path),
                        "knowledge-symlink",
                        "知识条目不能为符号链接",
                    )
                )
                continue
            if stat.S_ISDIR(mode):
                pending.append(path)
                continue
            if path.suffix.casefold() != ".md":
                continue
            if not stat.S_ISREG(mode):
                findings.append(
                    finding(
                        paths.relative(path),
                        "knowledge-document-not-regular",
                        "Markdown 知识必须为常规文件",
                    )
                )
                continue
            documents.append(path)

    return documents, findings


# 不存在的范围表示空库存，已存在但不安全的范围必须报告问题。
# 根目录本身也需验证，不能只保护其下条目。
# 文档清单、模块归属与发现一起返回，避免核验器重复扫描。
def discover_knowledge(paths: ModusPaths, root: Path) -> KnowledgeInventory:
    """Walk root without following links or accepting special files.

    A missing scope is empty; an unsafe or unreadable scope yields a finding.
    Documents are sorted independently of traversal order before Module identity
    is derived from canonical physical paths.
    """

    if root.is_symlink():
        return KnowledgeInventory(
            (),
            (),
            (
                finding(
                    paths.relative(root),
                    "knowledge-symlink",
                    "知识范围不能为符号链接",
                ),
            ),
        )
    if not root.exists():
        return KnowledgeInventory((), (), ())
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        return KnowledgeInventory(
            (),
            (),
            (
                finding(
                    paths.relative(root),
                    "knowledge-path-unreadable",
                    str(exc),
                ),
            ),
        )
    if not stat.S_ISDIR(root_mode):
        return KnowledgeInventory(
            (),
            (),
            (
                finding(
                    paths.relative(root),
                    "knowledge-path-invalid",
                    "知识范围必须为目录",
                ),
            ),
        )

    documents, findings = _scan_documents(paths, root)
    documents.sort()
    modules, module_findings = _module_documents(paths, documents)
    findings.extend(module_findings)
    return KnowledgeInventory(tuple(documents), tuple(modules), tuple(findings))


__all__ = [
    "KnowledgeInventory",
    "ModuleDocument",
    "discover_knowledge",
]
