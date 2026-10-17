"""Deterministic source-anchor verification for authored knowledge.

Checks compare authored anchors with current source. Findings distinguish
mechanically repairable drift from changes that need author review:

- ``line_drift``     the anchored symbol still exists in the file but on a
                     different line, and the match is unique -- safe to repair
                     mechanically (``--fix`` rewrites only the line number).
- ``symbol_missing`` the anchored symbol no longer occurs in the file (or the
                     file is gone) -- an editorial decision, never auto-fixed.
- ``ambiguous``      several lines match; a mechanical fix could silently pick
                     the wrong one, so it is reported and left alone.
- ``anchor_metadata_invalid`` malformed structured source anchors on any
                              knowledge document.
- ``ref_*``                  legacy inline ``path#L`` references in knowledge
                              bodies whose file is missing or whose line is out
                              of range.

Repair honesty: a fix updates the *anchor's claimed line* to where the symbol
verifiably is now.  The claimed line is never overwritten just to silence a
finding -- ``symbol_missing`` and ``ambiguous`` stay visible until an author
revises the document through a knowledge-maintenance workflow.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from ..paths import ModusPaths
from ..scope_models import SourceLocation
from ..skillmd import render, split_frontmatter

__all__ = ["run_verify_anchors"]

_REF_RE = re.compile(r"(?P<path>[\w][\w./-]*\.[A-Za-z0-9_]+)#L(?P<line>\d+)")

_MAX_SOURCE_BYTES = 8 * 1024 * 1024


def _identifier_stem(symbol: str) -> str:
    """The searchable identifier inside an authored anchor symbol."""
    cleaned = symbol.strip().strip("()").lstrip(".")
    cleaned = cleaned.split("(", 1)[0]
    return cleaned.rsplit(".", 1)[-1].rsplit("#", 1)[-1].strip()


# 按标识符边界收集全部命中，多个结果必须交给作者判断而非任选。
def _symbol_lines(source_lines: list[str], stem: str) -> list[int]:
    if not stem:
        return []
    pattern = re.compile(rf"(?<![\w]){re.escape(stem)}(?![\w])")
    return [index for index, line in enumerate(source_lines, start=1) if pattern.search(line)]


# 读取当前工作树并限制文件大小，验证结果不依赖历史分析缓存。
def _read_source(paths: ModusPaths, relative: str) -> list[str] | None:
    try:
        return read_bound_regular_text(
            paths.root, paths.root / relative, max_bytes=_MAX_SOURCE_BYTES
        ).splitlines()
    except (BoundKnowledgeReadError, OSError, ValueError):
        return None


def _in_domain(paths: ModusPaths, path: Path, domain: str | None) -> bool:
    """判断路径是否属于指定领域。"""

    if domain is None:
        return True
    try:
        path.relative_to(paths.domain(domain))
    except ValueError:
        return False
    return True


# 锚点是可选元数据；结构非法时逐项记录，避免一个坏条目遮蔽全部结果。
def _document_locations(
    paths: ModusPaths, path: Path
) -> tuple[list[SourceLocation], list[dict[str, Any]]]:
    """Read optional ``anchors.locations`` without assuming a document type."""

    document = paths.relative(path)
    try:
        text = read_bound_regular_text(paths.root, path, max_bytes=2 * 1024 * 1024)
    except (BoundKnowledgeReadError, OSError, ValueError):
        return [], []
    frontmatter, _body = split_frontmatter(text)
    anchors = frontmatter.get("anchors")
    if anchors is None:
        return [], []
    if not isinstance(anchors, dict):
        return [], [
            {
                "document": document,
                "finding": "anchor_metadata_invalid",
                "detail": "anchors 必须为映射",
            }
        ]
    raw_locations = anchors.get("locations")
    if raw_locations is None:
        return [], []
    if not isinstance(raw_locations, list):
        return [], [
            {
                "document": document,
                "finding": "anchor_metadata_invalid",
                "detail": "anchors.locations 必须为列表",
            }
        ]

    locations: list[SourceLocation] = []
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_locations):
        try:
            locations.append(SourceLocation.from_dict(raw))
        except (TypeError, ValueError) as exc:
            findings.append(
                {
                    "document": document,
                    "finding": "anchor_metadata_invalid",
                    "detail": f"anchors.locations[{index}]: {exc}",
                }
            )
    return locations, findings


def _verify_structured_anchors(
    paths: ModusPaths,
    *,
    fix: bool,
    source_cache: dict[str, list[str] | None],
    domain: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    from ..retrieval.stages.sections import markdown_files

    findings: list[dict[str, Any]] = []
    fixed_documents: list[str] = []
    for path in markdown_files(paths, "project"):
        if not _in_domain(paths, path, domain):
            continue
        document = paths.relative(path)
        locations, metadata_findings = _document_locations(paths, path)
        findings.extend(metadata_findings)
        repairs: dict[tuple[str, str, int], int] = {}
        for location in locations:
            if location.path not in source_cache:
                source_cache[location.path] = _read_source(paths, location.path)
            source_lines = source_cache[location.path]
            base = {
                "document": document,
                "path": location.path,
                "symbol": location.symbol,
                "claimedLine": location.start_line,
            }
            if source_lines is None:
                findings.append({**base, "finding": "symbol_missing", "detail": "文件缺失"})
                continue
            stem = _identifier_stem(location.symbol)
            matches = _symbol_lines(source_lines, stem)
            if not matches:
                findings.append({**base, "finding": "symbol_missing", "detail": "文件中未找到符号"})
                continue
            if location.start_line in matches:
                continue
            if len(matches) == 1:
                findings.append(
                    {
                        **base,
                        "finding": "line_drift",
                        "actualLine": matches[0],
                        "fixable": True,
                    }
                )
                repairs[(location.path, location.symbol, location.start_line)] = matches[0]
            else:
                findings.append(
                    {
                        **base,
                        "finding": "ambiguous",
                        "candidateLines": matches[:10],
                        "detail": "匹配到多个行，无法自动修复",
                    }
                )
        if fix and repairs:
            if _repair_document(paths, document, repairs):
                fixed_documents.append(document)
                for finding in findings:
                    key = (
                        finding.get("path"),
                        finding.get("symbol"),
                        finding.get("claimedLine"),
                    )
                    if finding.get("document") == document and key in repairs:
                        finding["fixed"] = True
    return findings, fixed_documents


# 机械修复仅更新可唯一定位的行号，写入依赖原始内容摘要检测并发编辑。
def _repair_document(
    paths: ModusPaths, document: str, repairs: dict[tuple[str, str, int], int]
) -> bool:
    """Rewrite only the drifted startLine values, under the shared CAS writer."""
    from ..knowledge_write_session import write_document

    try:
        text = read_bound_regular_text(paths.root, paths.root / document)
    except (BoundKnowledgeReadError, OSError, ValueError):
        return False
    frontmatter, body = split_frontmatter(text)
    anchors = frontmatter.get("anchors")
    if not isinstance(anchors, dict):
        return False
    locations = anchors.get("locations")
    if not isinstance(locations, list):
        return False
    changed = False
    for item in locations:
        if not isinstance(item, dict):
            continue
        key = (item.get("path"), item.get("symbol"), item.get("startLine"))
        if key in repairs:
            offset = repairs[key] - int(item.get("startLine") or 0)
            item["startLine"] = repairs[key]
            if isinstance(item.get("endLine"), int):
                item["endLine"] = max(item["startLine"], item["endLine"] + offset)
            changed = True
    if not changed:
        return False
    try:
        write_document(
            paths,
            document,
            render(frontmatter, body),
            expected_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def _verify_body_references(
    paths: ModusPaths,
    source_cache: dict[str, list[str] | None],
    *,
    domain: str | None,
) -> list[dict[str, Any]]:
    from ..retrieval.stages.sections import markdown_files

    findings: list[dict[str, Any]] = []
    for path in markdown_files(paths, "project"):
        if not _in_domain(paths, path, domain):
            continue
        try:
            text = read_bound_regular_text(paths.root, path, max_bytes=2 * 1024 * 1024)
        except (BoundKnowledgeReadError, OSError, ValueError):
            continue
        document = paths.relative(path)
        _frontmatter, body = split_frontmatter(text)
        for match in _REF_RE.finditer(body):
            relative = match.group("path")
            line = int(match.group("line"))
            if not (paths.root / relative).exists():
                findings.append(
                    {
                        "document": document,
                        "finding": "ref_file_missing",
                        "reference": match.group(0),
                    }
                )
                continue
            if relative not in source_cache:
                source_cache[relative] = _read_source(paths, relative)
            source_lines = source_cache[relative]
            if source_lines is not None and line > len(source_lines):
                findings.append(
                    {
                        "document": document,
                        "finding": "ref_line_out_of_range",
                        "reference": match.group(0),
                        "fileLines": len(source_lines),
                    }
                )
    return findings


def run_verify_anchors(
    root: str = ".", *, fix: bool = False, domain: str | None = None
) -> dict[str, Any]:
    """Verify structured anchors and legacy body references; optionally repair drift."""
    paths = ModusPaths.for_root(root)
    source_cache: dict[str, list[str] | None] = {}
    anchor_findings, fixed_documents = _verify_structured_anchors(
        paths, fix=fix, source_cache=source_cache, domain=domain
    )
    reference_findings = _verify_body_references(paths, source_cache, domain=domain)
    findings = [*anchor_findings, *reference_findings]
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["finding"]] = counts.get(finding["finding"], 0) + 1
    return {
        "status": "clean" if not findings else "findings",
        "findings": findings,
        "counts": counts,
        "fixedDocuments": fixed_documents,
        "checkedDocuments": len({finding["document"] for finding in findings})
        if findings
        else None,
    }
