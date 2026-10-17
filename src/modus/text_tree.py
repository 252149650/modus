"""ASCII directory trees for human CLI reports.

Shared by project installation and status reports so connector glyphs stay identical:
``├──`` / ``│`` / ``└──``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TreeNode:
    """One immutable row in a human-facing tree."""

    name: str
    note: str = ""
    children: tuple[TreeNode, ...] = ()


def display_width(text: str) -> int:
    """Return terminal columns, accounting for East Asian wide characters."""

    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _pad_to(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def walk_tree(nodes: Sequence[TreeNode], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten ``nodes`` into connector-prefixed paths and optional notes."""

    rows: list[tuple[str, str]] = []
    last_index = len(nodes) - 1
    for index, node in enumerate(nodes):
        last = index == last_index
        branch = "└── " if last else "├── "
        rows.append((f"{prefix}{branch}{node.name}", node.note))
        if node.children:
            child_prefix = f"{prefix}{'    ' if last else '│   '}"
            rows.extend(walk_tree(node.children, child_prefix))
    return rows


def render_tree(nodes: Sequence[TreeNode], *, indent: str = "  ") -> str:
    """Render a column-aligned Unicode tree for terminal output."""

    rows = [(f"{indent}{path}", note) for path, note in walk_tree(nodes)]
    if not rows:
        return ""
    note_column = max(display_width(path) for path, _note in rows) + 2
    lines: list[str] = []
    for path, note in rows:
        lines.append(f"{_pad_to(path, note_column)}{note}" if note else path)
    return "\n".join(lines)


__all__ = ["TreeNode", "display_width", "render_tree", "walk_tree"]
