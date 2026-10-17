"""命令报告共享的展示原语。

预览与结果统一使用路径树和提示分隔线。文件、目录、path#marker 受管区块
都在此规范化，避免报告模块彼此依赖。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..text_tree import TreeNode
from ..text_tree import render_tree as _render_tree

__all__ = [
    "NOTICE_SEPARATOR",
    "TreeNode",
    "build_path_tree",
    "format_path_tree",
    "render_tree",
]

NOTICE_SEPARATOR = "─" * 40


def render_tree(nodes: tuple[TreeNode, ...], *, indent: str = "") -> str:
    """Render flush-left (reports own their margins), unlike the shared default."""
    return _render_tree(nodes, indent=indent)


# 构建期使用可变子节点表去重，输出时冻结为共享树模型。
@dataclass
class _MutableNode:
    """Build-time tree node; frozen into :class:`TreeNode` once assembled."""

    name: str
    note: str = ""
    is_dir_leaf: bool = False
    children: dict[str, _MutableNode] = field(default_factory=dict)


def _child(parent: _MutableNode, name: str) -> _MutableNode:
    node = parent.children.get(name)
    if node is None:
        node = _MutableNode(name=name)
        parent.children[name] = node
    return node


# 报告统一路径分隔符，仅用于展示，不承担文件系统授权。
def _split_rel(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").strip("/").split("/") if part]


def _insert_path(root: _MutableNode, path: str, *, is_dir: bool, note: str = "") -> None:
    parts = _split_rel(path)
    if not parts:
        return
    node = root
    for part in parts[:-1]:
        node = _child(node, part)
    leaf = _child(node, parts[-1])
    if note:
        leaf.note = note
    if is_dir:
        leaf.is_dir_leaf = True


# 冻结时按名称排序，并为目录补显示后缀，保持报告顺序稳定。
def _freeze(node: _MutableNode) -> TreeNode:
    children = tuple(
        _freeze(child) for child in sorted(node.children.values(), key=lambda item: item.name)
    )
    name = node.name
    if (children or node.is_dir_leaf) and not name.endswith("/"):
        name = f"{name}/"
    return TreeNode(name=name, note=node.note, children=children)


# 文件、目录和标记区块共用一棵路径树，避免重复展示共享父目录。
def build_path_tree(
    files: Sequence[str] = (),
    dirs: Sequence[str] = (),
    sections: Sequence[str] = (),
    *,
    file_notes: dict[str, str] | None = None,
) -> tuple[TreeNode, ...]:
    """Build an ASCII-tree from mixed file, directory, and ``path#marker`` entries."""
    root = _MutableNode(name="")
    notes = dict(file_notes or {})
    for entry in sections:
        if "#" in entry:
            path, marker = entry.rsplit("#", 1)
            notes[path] = f"[{marker}]"
            _insert_path(root, path, is_dir=False, note=notes[path])
        else:
            _insert_path(root, entry, is_dir=False, note=notes.get(entry, "[受管区块]"))
    for path in files:
        _insert_path(root, path, is_dir=False, note=notes.get(path, ""))
    for path in dirs:
        _insert_path(root, path, is_dir=True, note=notes.get(path.rstrip("/"), ""))
    return tuple(
        _freeze(child) for child in sorted(root.children.values(), key=lambda item: item.name)
    )


# 组合入口复用构建与渲染原语，所有命令保持同一树形风格。
def format_path_tree(
    files: Sequence[str] = (),
    dirs: Sequence[str] = (),
    sections: Sequence[str] = (),
    *,
    file_notes: dict[str, str] | None = None,
) -> str:
    return render_tree(build_path_tree(files, dirs, sections, file_notes=file_notes))
