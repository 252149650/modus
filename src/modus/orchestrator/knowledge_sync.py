"""Inspect which narrative knowledge may be affected by source changes.

This service is intentionally read-only and independent of initialization analysis:
it combines explicit targets, source-location anchors, and a direct read of
*current* source into review candidates. It never imports or reads an
initialization bundle, so a sync check is exactly as
trustworthy as the source tree the agent already has checked out. The agent
remains responsible for reading current source and for performing any
compare-and-swap knowledge update.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from ..knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from ..paths import ModusPaths
from ..scope_models import ModuleMeta
from .models import JsonObject
from .path_scope import relative_paths

_MAX_KNOWLEDGE_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_DELIVERED_DIAGNOSTICS = 32
_VALID_VALIDATION_STATES = frozenset({"passed", "failed"})

__all__ = ["inspect_knowledge_impact", "sync_knowledge"]


@dataclass(frozen=True, slots=True)
class _ModuleImpact:
    """One module candidate plus any anchor losses requiring a decision."""

    candidate: JsonObject | None
    anchor_reviews: tuple[JsonObject, ...] = ()


# 影响检查只提供候选与证据缺口，失败验证不授权把新行为写成已确认知识。
def inspect_knowledge_impact(
    root: str,
    changed_files: Sequence[str] = (),
    *,
    targets: Sequence[str] = (),
    validation: str = "passed",
    _local: bool = False,
) -> JsonObject:
    """Return a deterministic, read-only knowledge-impact report."""
    if validation not in _VALID_VALIDATION_STATES:
        raise ValueError("validation 必须为 passed 或 failed")

    paths = ModusPaths.for_root(root)
    changed = relative_paths(paths, changed_files)
    selected_targets = relative_paths(paths, targets)
    if not _local:
        workspace_result = _delegate_workspace_if_needed(
            paths,
            changed,
            selected_targets,
            validation=validation,
        )
        if workspace_result is not None:
            return workspace_result

    diagnostics: list[JsonObject] = []
    candidates, anchor_reviews = _candidate_modules(
        paths,
        changed,
        selected_targets,
        diagnostics=diagnostics,
    )
    conflicts = _target_conflicts(paths, candidates)
    skipped: list[JsonObject] = []

    if validation == "failed":
        diagnostics.append(
            {
                "files": changed,
                "kind": "implementation-validation-failed",
                "reason": (
                    "实现验证失败；仍可维护已确立的事实和已记录的失败，但不能将未验证行为作为已确认结果"
                ),
            }
        )
    if changed and not candidates:
        skipped.append(
            {
                "files": changed,
                "reason": "没有可靠的知识目标，助手需先确定业务归属",
            }
        )

    return {
        "status": "partial" if conflicts else "noop",
        "mode": "agent-authored",
        "changedFiles": changed,
        "candidates": candidates,
        "lowConfidenceCandidates": [
            candidate["path"] for candidate in candidates if candidate["confidence"] == "path-only"
        ],
        "anchorReviewCandidates": anchor_reviews,
        "writes": [],
        "skipped": skipped,
        "conflicts": conflicts,
        "diagnostics": diagnostics[:_MAX_DELIVERED_DIAGNOSTICS],
        "diagnosticCount": len(diagnostics),
        "validation": validation,
        "message": (
            "只读影响检查完成。助手需评审需求、差异、测试与当前知识，再直接修订已有语义章节。"
        ),
    }


def sync_knowledge(
    root: str,
    changed_files: Sequence[str] = (),
    *,
    targets: Sequence[str] = (),
    validation: str = "passed",
    _local: bool = False,
) -> JsonObject:
    """Compatibility entry point for the historical knowledge-sync command."""
    return inspect_knowledge_impact(
        root,
        changed_files,
        targets=targets,
        validation=validation,
        _local=_local,
    )


# 工作区分派由专用服务处理，成员调用通过本地标记避免再次递归。
def _delegate_workspace_if_needed(
    paths: ModusPaths,
    changed: list[str],
    targets: list[str],
    *,
    validation: str,
) -> JsonObject | None:
    from ..config import ModusConfig

    try:
        config = ModusConfig.read_available(paths)
    except (OSError, ValueError):
        config = ModusConfig()
    if config.kind != "workspace":
        return None

    from ..workspace import sync_knowledge as sync_workspace

    workspace_sync = cast(Callable[..., JsonObject], sync_workspace)
    return workspace_sync(
        str(paths.root),
        changed,
        targets=targets,
        validation=validation,
    )


# 候选按文档路径去重，显式非模块目标仍保留而不捏造模块归属。
def _candidate_modules(
    paths: ModusPaths,
    changed: Sequence[str],
    targets: Sequence[str],
    *,
    diagnostics: list[JsonObject] | None = None,
) -> tuple[list[JsonObject], list[JsonObject]]:
    from .module_meta import list_modules

    explicit_targets = set(targets)
    changed_paths = set(changed)
    modules = list_modules(paths, diagnostics=diagnostics)
    source_cache: dict[str, list[str] | None] = {}

    candidates: dict[str, JsonObject] = {}
    anchor_reviews: list[JsonObject] = []
    for metadata in modules:
        impact = _module_impact(
            paths,
            metadata,
            changed_paths=changed_paths,
            explicit_targets=explicit_targets,
            source_cache=source_cache,
        )
        if impact.candidate is None:
            continue
        module_path = cast(str, impact.candidate["path"])
        candidates[module_path] = impact.candidate
        anchor_reviews.extend(impact.anchor_reviews)

    # An explicit non-Module document remains visible without inventing
    # business ownership for it.
    for target in targets:
        candidates.setdefault(target, _explicit_candidate(target))
    return [candidates[key] for key in sorted(candidates)], anchor_reviews


# 路径命中、符号缺失和显式选择分别保留原因，置信度可追溯。
def _module_impact(
    paths: ModusPaths,
    metadata: ModuleMeta,
    *,
    changed_paths: set[str],
    explicit_targets: set[str],
    source_cache: dict[str, list[str] | None],
) -> _ModuleImpact:
    module_path = _module_path(paths, metadata)
    matched_paths = sorted(
        {location.path for location in metadata.anchor_locations if location.path in changed_paths}
    )
    matched_symbols: list[JsonObject] = []
    lost_anchors: list[JsonObject] = []
    for location in metadata.anchor_locations:
        if location.path not in changed_paths and module_path not in explicit_targets:
            continue
        loss = _anchor_loss(paths, location.path, location.symbol, source_cache)
        if loss is None:
            continue
        record: JsonObject = {"path": location.path, "anchorSymbol": location.symbol, **loss}
        matched_symbols.append(record)
        lost_anchors.append(record)

    explicitly_selected = module_path in explicit_targets
    if not explicitly_selected and not matched_paths and not matched_symbols:
        return _ModuleImpact(candidate=None)

    reasons: list[str] = []
    confidence = "path-only"
    if matched_paths:
        reasons.append("source-location-path")
    if matched_symbols:
        reasons.append("source-location-symbol")
        confidence = "symbol"
    if explicitly_selected:
        reasons.append("explicit-target")
        confidence = "explicit"

    candidate: JsonObject = {
        "path": module_path,
        "moduleId": str(metadata.module_id),
        "confidence": confidence,
        "reasons": reasons,
        "matchedSourcePaths": matched_paths,
        "matchedSymbols": matched_symbols,
    }
    reviews = tuple(
        {
            "moduleId": str(metadata.module_id),
            "document": module_path,
            **record,
            "reason": (
                "直接读取当前源码时已找不到锚定符号；请先核查文件，再修订文档或记录证据缺口"
            ),
        }
        for record in lost_anchors
    )
    return _ModuleImpact(candidate=candidate, anchor_reviews=reviews)


# 直接检查当前源码；行号移动但符号仍存在时不判为丢失。
def _anchor_loss(
    paths: ModusPaths,
    relative_path: str,
    symbol: str,
    source_cache: dict[str, list[str] | None],
) -> JsonObject | None:
    """Report an anchor as lost only on direct evidence from current source.

    This mirrors the mechanical, script-first check in
    ``modus knowledge verify-anchors``: no persisted analysis diff is consulted, so
    the verdict is exactly as current as the checked-out working tree. A
    missing file or an identifier stem absent from every line is reported as
    ``anchor-symbol-missing``; the anchor's own claimed line is irrelevant
    here, because moved-but-still-present symbols are drift, not loss, and are
    already handled by ``verify-anchors --fix``.
    """
    if relative_path not in source_cache:
        source_cache[relative_path] = _read_source_lines(paths, relative_path)
    source_lines = source_cache[relative_path]
    if source_lines is None:
        return {"disposition": "anchor-symbol-missing", "detail": "文件缺失"}
    stem = _identifier_stem(symbol)
    if stem and _stem_present(source_lines, stem):
        return None
    return {"disposition": "anchor-symbol-missing", "detail": "文件中未找到符号"}


# 统一读取边界和大小上限，单次检查共享源码缓存。
def _read_source_lines(paths: ModusPaths, relative_path: str) -> list[str] | None:
    try:
        return read_bound_regular_text(
            paths.root, paths.root / relative_path, max_bytes=_MAX_SOURCE_BYTES
        ).splitlines()
    except (BoundKnowledgeReadError, OSError, ValueError):
        return None


def _identifier_stem(symbol: str) -> str:
    """The searchable identifier inside an authored anchor symbol."""
    cleaned = symbol.strip().strip("()").lstrip(".")
    cleaned = cleaned.split("(", 1)[0]
    return cleaned.rsplit(".", 1)[-1].rsplit("#", 1)[-1].strip()


# 按标识符边界匹配，避免短名称误命中更长的无关符号。
def _stem_present(source_lines: list[str], stem: str) -> bool:
    pattern = re.compile(rf"(?<![\w]){re.escape(stem)}(?![\w])")
    return any(pattern.search(line) for line in source_lines)


# 候选目标不可读或缺失时报告冲突，不能悄悄创建替代文档。
def _target_conflicts(paths: ModusPaths, candidates: Sequence[JsonObject]) -> list[JsonObject]:
    conflicts: list[JsonObject] = []
    for candidate in candidates:
        target = str(candidate["path"])
        try:
            text = _read(paths, target)
        except (BoundKnowledgeReadError, OSError, ValueError) as exc:
            conflicts.append({"path": target, "reason": str(exc)})
            continue
        if text is None:
            conflicts.append({"path": target, "reason": "知识目标缺失"})
    return conflicts


# 缺失与其他读取故障分开处理，保留错误语义供上层展示。
def _read(paths: ModusPaths, relative: str) -> str | None:
    try:
        return read_bound_regular_text(
            paths.root,
            paths.root / relative,
            max_bytes=_MAX_KNOWLEDGE_BYTES,
        )
    except BoundKnowledgeReadError as exc:
        if exc.kind == "missing":
            return None
        raise


def _module_path(paths: ModusPaths, metadata: ModuleMeta) -> str:
    return paths.relative(paths.module(metadata.module_id.domain, metadata.module_id.module))


# 显式目标代表用户选择，不推断额外的源码匹配证据。
def _explicit_candidate(target: str) -> JsonObject:
    return {
        "path": target,
        "moduleId": None,
        "confidence": "explicit",
        "reasons": ["explicit-target"],
        "matchedSourcePaths": [],
        "matchedSymbols": [],
    }
