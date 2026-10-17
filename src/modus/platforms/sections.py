"""Safe editing primitives for Modus-owned sections in shared text files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SectionIntegrityError(ValueError):
    """A shared file contains incomplete, duplicated, or inverted markers."""


def section_markers(marker: str) -> tuple[str, str]:
    """Return the BEGIN/END comment pair for one Modus ownership marker."""

    if not marker or "\n" in marker or "-->" in marker:
        raise ValueError(f"受管区块标记无效： {marker!r}")
    return f"<!-- MODUS:BEGIN {marker} -->", f"<!-- MODUS:END {marker} -->"


@dataclass(frozen=True, slots=True)
class _SectionSpan:
    start: int
    stop: int


def _locate_section(text: str, marker: str, *, source: object) -> _SectionSpan | None:
    begin, end = section_markers(marker)
    begin_count = text.count(begin)
    end_count = text.count(end)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise SectionIntegrityError(f"受管区块 {marker!r} in {source} 包含不完整或重复的标记")

    start = text.index(begin)
    end_start = text.index(end)
    if start >= end_start:
        raise SectionIntegrityError(f"受管区块 {marker!r} in {source} 的标记顺序颠倒")
    return _SectionSpan(start=start, stop=end_start + len(end))


def section_block(path: Path, marker: str) -> str | None:
    """Read a complete managed block, including markers, when one exists.

    A malformed marker pair is never interpreted as an absent section: callers
    receive :class:`SectionIntegrityError` and can preserve or report the file
    instead of appending a second, ambiguous ownership block.
    """

    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    span = _locate_section(text, marker, source=path)
    return None if span is None else text[span.start : span.stop]


def render_section(marker: str, content: str) -> str:
    """Render deterministic managed-section text without a trailing newline."""

    begin, end = section_markers(marker)
    return f"{begin}\n{content.rstrip(chr(10))}\n{end}"


def inject_section(path: Path, marker: str, content: str) -> str:
    """Insert or replace one 受管区块 while preserving surrounding text."""

    block = render_section(marker, content)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    span = _locate_section(text, marker, source=path)
    if span is not None:
        new_text = text[: span.start] + block + text[span.stop :]
    elif text.strip():
        new_text = text.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new_text = block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return block


def remove_section(path: Path, marker: str) -> None:
    """删除一个受管区块，逐字保留周围内容。"""

    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    span = _locate_section(text, marker, source=path)
    if span is None:
        return

    before = text[: span.start]
    after = text[span.stop :]
    new_text = (before.rstrip("\n") + "\n" + after.lstrip("\n")).strip("\n")
    if new_text:
        path.write_text(new_text + "\n", encoding="utf-8")
    else:
        path.unlink()
