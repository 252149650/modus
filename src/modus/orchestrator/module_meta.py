"""Read physically bound Module metadata from canonical knowledge documents.

Markdown remains useful when optional frontmatter is absent or partially
invalid. Identity fields are the exception: a document may never claim a
Module identity that disagrees with its canonical filesystem location.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeAlias

from ..knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from ..paths import ModusPaths
from ..scope_models import (
    MODULE_META_SCHEMA_VERSION,
    ModuleId,
    ModuleMeta,
    module_meta_field_names,
)
from ..skillmd import split_frontmatter
from .models import JsonObject

Diagnostics: TypeAlias = list[JsonObject]
_IGNORED_DIRECTORY_ENTRIES = frozenset({".gitkeep", ".DS_Store"})

__all__ = ["list_modules", "load", "load_complete", "load_module"]


# 文档声明必须与磁盘位置一致，元数据不能把文件归属重定向到其他模块。
def _physical_errors(data: Mapping[str, Any], physical: ModuleId) -> list[str]:
    """Keep declared metadata bound to the document's canonical location."""
    try:
        declared = ModuleId.parse(data.get("moduleId", ""))
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    if declared != physical:
        errors.append(f"模块 frontmatter 身份 {declared} 与实际路径不匹配：{physical}")
    if data.get("parent") != physical.domain:
        errors.append(f"模块 frontmatter 的 parent 必须匹配实际父领域 {physical.domain}")
    return errors


# 可选元数据损坏时仍可读取正文；身份字段冲突则直接拒绝。
# 旧状态只降级为观察状态，不能冒充人工确认。
# 业务自定义字段保留在文档中，构造 ModuleMeta 时仅接收 schema 拥有的字段。
def load_module(
    paths: ModusPaths,
    domain: str,
    module: str,
    *,
    diagnostics: Diagnostics | None = None,
) -> ModuleMeta | None:
    """Read available metadata; a plain document needs no metadata scaffold."""
    physical = ModuleId(domain, module)
    document = paths.module(domain, module)
    try:
        text = read_bound_regular_text(paths.root, document)
    except BoundKnowledgeReadError as exc:
        if exc.kind == "missing":
            return None
        raise ValueError(str(exc)) from exc
    frontmatter, body = split_frontmatter(text)
    if not frontmatter and body == text and text.startswith("---") and diagnostics is not None:
        diagnostics.append(
            {
                "path": paths.relative(document),
                "kind": "module-metadata-unreadable",
                "reason": "无法读取 frontmatter，Markdown 正文仍可使用",
            }
        )
    data: JsonObject = {key: value for key, value in frontmatter.items() if key != "modus"}
    data.setdefault("schemaVersion", MODULE_META_SCHEMA_VERSION)
    data.setdefault("state", "observed")
    data.setdefault("moduleId", str(physical))
    data.setdefault("parent", physical.domain)
    errors = _physical_errors(data, physical)
    if errors:
        raise ValueError("; ".join(errors))
    data.setdefault("responsibilities", body[:500])
    # Earlier runtime templates emitted these shapes. Keep their useful anchors
    # when reading, without treating an old `active` label as human confirmation.
    if data.get("state") == "active":
        data["state"] = "observed"
    if isinstance(data.get("responsibilities"), list):
        data["responsibilities"] = "；".join(str(item) for item in data["responsibilities"])
    metadata: JsonObject = {
        key: value for key, value in data.items() if key in module_meta_field_names()
    }
    try:
        return ModuleMeta.from_dict(metadata)
    except (ValueError, TypeError) as exc:
        if diagnostics is not None:
            diagnostics.append(
                {
                    "path": paths.relative(document),
                    "kind": "module-metadata-partial",
                    "reason": str(exc),
                }
            )
        return ModuleMeta(module_id=physical, responsibilities=body[:500])


def load(
    paths: ModusPaths,
    domain: str,
    module: str,
    *,
    diagnostics: Diagnostics | None = None,
) -> ModuleMeta | None:
    """Backward-compatible alias for :func:`load_module`."""
    return load_module(paths, domain, module, diagnostics=diagnostics)


def load_complete(
    paths: ModusPaths,
    domain: str,
    module: str,
    *,
    diagnostics: Diagnostics | None = None,
) -> ModuleMeta | None:
    """Backward-compatible alias for :func:`load_module`."""
    return load_module(paths, domain, module, diagnostics=diagnostics)


# 目录异常作为诊断返回，其他领域仍可继续枚举。
def _children(paths: ModusPaths, directory: Path, diagnostics: Diagnostics | None) -> list[Path]:
    try:
        if directory.is_symlink():
            raise ValueError("模块目录不能为符号链接")
        if not directory.is_dir():
            return []
        return sorted(directory.iterdir())
    except (OSError, ValueError) as exc:
        if diagnostics is not None:
            diagnostics.append(
                {
                    "path": paths.relative(directory),
                    "kind": "module-directory-unavailable",
                    "reason": str(exc),
                }
            )
        return []


# 只接受规范模块目录中的常规 Markdown 文件，跳过链接和其他文件类型。
# 按规范身份排序，结果不依赖文件系统的枚举顺序。
def list_modules(
    paths: ModusPaths,
    *,
    domain: str | None = None,
    diagnostics: Diagnostics | None = None,
) -> list[ModuleMeta]:
    """List validated Modules in stable canonical identity order."""
    if domain is None:
        domains = [
            child.name
            for child in _children(paths, paths.domain_dir, diagnostics)
            if not child.is_symlink() and child.is_dir()
        ]
    else:
        domains = [domain]
    modules: list[ModuleMeta] = []
    for parent in domains:
        directory = paths.modules_dir(parent)
        for child in _children(paths, directory, diagnostics):
            if child.name in _IGNORED_DIRECTORY_ENTRIES:
                continue
            if child.suffix != ".md":
                continue
            if child.is_symlink() or not child.is_file():
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "path": paths.relative(child),
                            "kind": "module-path-unsafe",
                            "reason": "模块元数据必须从本地常规文件读取",
                        }
                    )
                continue
            try:
                meta = load_module(paths, parent, child.stem, diagnostics=diagnostics)
            except (OSError, ValueError, TypeError) as exc:
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "path": paths.relative(child),
                            "kind": "module-metadata-unavailable",
                            "reason": str(exc),
                        }
                    )
                continue
            if meta is not None:
                modules.append(meta)
    return sorted(modules, key=lambda meta: str(meta.module_id))
