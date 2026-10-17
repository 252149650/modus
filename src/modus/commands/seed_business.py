"""Create canonical, missing-only human business indexes for confirmed Domains."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import ModusConfig
from ..knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from ..knowledge_seeds import domain_business_seed_files
from ..knowledge_write_session import write_document
from ..paths import ModusPaths


# 业务索引归成员项目所有，外层工作区只提供导航，不代写成员知识。
def _require_project(paths: ModusPaths) -> None:
    if paths.config_path.is_file() and ModusConfig.read(paths).kind == "workspace":
        raise ValueError("领域业务种子仅适用于项目，不适用于工作区")


# 要求已有常规域入口，以免拼错域名时悄悄创建新的知识分类。
def _require_confirmed_domain(paths: ModusPaths, domain: str) -> None:
    skill = paths.domain_skill(domain)
    if skill.is_symlink() or not skill.is_file():
        raise ValueError(f"Domain {domain!r} 必须先有常规 SKILL.md 文件才能创建业务种子")


# 种子只填补缺失文档，已经存在的字节无论内容如何都交给作者维护。
# 安全读取区分不存在和不可安全访问，后者不能按空文件处理。
# 写入使用 expected_hash=None 再确认目标不存在，保护预检后的并发创建。
def seed_domain_business(root: str | Path, domains: Iterable[str]) -> dict[str, Any]:
    """Create each Domain's four indexes without changing existing human bytes."""
    paths = ModusPaths.for_root(root)
    _require_project(paths)
    selected = tuple(dict.fromkeys(domains))
    if not selected:
        raise ValueError("至少需要一个 --domain")

    created: list[str] = []
    preserved: list[str] = []
    for domain in selected:
        _require_confirmed_domain(paths, domain)
        for target, text in domain_business_seed_files(paths, domain).items():
            relative = paths.relative(target)
            try:
                read_bound_regular_text(paths.root, target)
            except BoundKnowledgeReadError as exc:
                if exc.kind != "missing":
                    raise ValueError(f"业务种子目标不安全： {relative}: {exc.kind}") from exc
                write_document(
                    paths,
                    relative,
                    text,
                    expected_hash=None,
                    allow_human=True,
                )
                created.append(relative)
            else:
                preserved.append(relative)
    return {
        "schemaVersion": 1,
        "status": "changed" if created else "unchanged",
        "domains": list(selected),
        "created": created,
        "preserved": preserved,
    }


__all__ = ["seed_domain_business"]
