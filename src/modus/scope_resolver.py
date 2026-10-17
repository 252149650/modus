"""Reconstruct the current Scope catalog from authoritative Markdown.

This is intentionally a tolerant read model: one unreadable or malformed
document is skipped so retrieval can continue from the remaining knowledge.
It never repairs files and never treats a generated cache as Scope authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias

from .knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from .paths import ModusPaths
from .skillmd import split_frontmatter

JsonObject: TypeAlias = dict[str, Any]


def _string_values(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return sorted({item for item in value if isinstance(item, str)})


def _anchor_locations(value: object) -> list[JsonObject]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


# 物理路径提供稳定模块 ID，可选 frontmatter 补充描述和锚点。
# locations 中的路径与符号并入检索词汇，避免精确锚点无法被粗粒度路由发现。
def _module_row(
    paths: ModusPaths,
    path: Path,
    *,
    domain: str,
    frontmatter: Mapping[str, Any],
    body: str,
    anchors: Mapping[str, Any],
) -> JsonObject:
    """Normalize one Module document into the retrieval catalog wire."""

    locations = _anchor_locations(anchors.get("locations"))
    anchor_paths = set(_string_values(anchors.get("paths")))
    anchor_symbols = set(_string_values(anchors.get("symbols")))
    anchor_paths.update(
        str(item["path"]) for item in locations if isinstance(item.get("path"), str)
    )
    anchor_symbols.update(
        str(item["symbol"]) for item in locations if isinstance(item.get("symbol"), str)
    )
    return {
        "moduleId": f"{domain}/{path.stem}",
        "parent": domain,
        "state": frontmatter.get("state", "confirmed"),
        "description": str(frontmatter.get("description") or body[:300]),
        "document": paths.relative(path),
        "anchorPaths": sorted(anchor_paths),
        "anchorSymbols": sorted(anchor_symbols),
        "anchorLocations": locations,
    }


# 每次从当前 Markdown 重建目录，生成缓存不能取代作者的组织决定。
# 坏单篇文档只减少可用检索面，不触发自动修复或阻止其他域读取。
def read_scope_products(paths: ModusPaths) -> JsonObject:
    """Derive organization from current readable documents."""
    from .retrieval.stages.sections import markdown_files

    products: JsonObject = {}
    domains: dict[str, JsonObject] = {}
    modules: dict[str, JsonObject] = {}
    for path in markdown_files(paths, "project"):
        relative = path.relative_to(paths.knowledge_dir)
        if len(relative.parts) < 3 or relative.parts[0] != "domain":
            continue
        domain = relative.parts[1]
        try:
            text = read_bound_regular_text(paths.root, path, max_bytes=2 * 1024 * 1024)
            frontmatter, body = split_frontmatter(text)
        except (BoundKnowledgeReadError, OSError, ValueError):
            continue
        domains.setdefault(domain, {"name": domain, "description": "", "anchorPaths": []})
        anchors = frontmatter.get("anchors") or {}
        if not isinstance(anchors, Mapping):
            anchors = {}
        if relative.parts[-1] == "SKILL.md":
            domains[domain].update(
                description=str(frontmatter.get("description") or body[:300]),
                anchorPaths=_string_values(anchors.get("paths")),
            )
        if relative.parts[2:4] == ("references", "modules") and len(relative.parts) == 5:
            module_id = f"{domain}/{path.stem}"
            modules[module_id] = _module_row(
                paths,
                path,
                domain=domain,
                frontmatter=frontmatter,
                body=body,
                anchors=anchors,
            )
    products["module-catalog"] = {
        "status": "ready" if modules else "degraded",
        "modules": list(modules.values()),
    }
    products["domains"] = list(domains.values())
    return products


__all__ = ["read_scope_products"]
