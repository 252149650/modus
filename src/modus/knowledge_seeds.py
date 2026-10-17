"""创作诊断与检索共享的精确种子识别。

文件存在、所有权注释和清单哈希都不能证明文档仍是种子。只接受与当前
随包模板完全一致的正文，其他内容均保留。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from .knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from .paths import ModusPaths
from .scaffold.catalog import TemplateCatalog

_TEAM_STANDARD_PATH: Final = "modus/knowledge/constraint/team-standard.md"
_GLOBAL_STANDARD_PATH: Final = "modus/knowledge/constraint/global-standard.md"
_CONSTRAINT_SEED_PATHS: Final = frozenset({_TEAM_STANDARD_PATH, _GLOBAL_STANDARD_PATH})


def is_constraint_seed(relative: str, text: str) -> bool:
    """仅将与当前随包约束模板完全一致的正文识别为种子。"""

    if relative not in _CONSTRAINT_SEED_PATHS:
        return False
    template = (
        Path(__file__).parent / "scaffold/templates/knowledge/constraint" / Path(relative).name
    )
    return text == template.read_text(encoding="utf-8")


def inspect_team_standard(paths: ModusPaths) -> dict[str, object]:
    """Read-only content state, not lifecycle detection or write permission."""
    relative = _TEAM_STANDARD_PATH
    try:
        text = read_bound_regular_text(paths.root, paths.team_standard)
    except BoundKnowledgeReadError as exc:
        if exc.kind == "missing":
            return {"path": relative, "state": "missing", "expectedHash": None}
        return {"path": relative, "state": "unavailable", "reason": exc.kind}
    return {
        "path": relative,
        "state": "framework-seed" if is_constraint_seed(relative, text) else "retained-content",
        "expectedHash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def domain_business_seed_files(paths: ModusPaths, domain: str) -> dict[Path, str]:
    """Return the four missing-only seed targets for one canonical Domain."""
    catalog = TemplateCatalog()
    return {
        paths.domain_business_index(domain, category): catalog.read(
            f"knowledge/business/{category + '/' if category else ''}index.md"
        )
        for category in (None, "meta", "experience", "scenario")
    }


__all__ = ["domain_business_seed_files", "inspect_team_standard", "is_constraint_seed"]
