"""Read-only source inventory for one initialization analysis."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from modus.knowledge_reader import BoundKnowledgeReadError, read_bound_regular_bytes
from modus.machine.corpus.source_filters import (
    is_ignored,
    is_noise_directory,
    is_sensitive,
    parse_gitignore_line,
)
from modus.paths import MANAGED_ROOT_FILES, PROJECT_OUTPUT_DIRS

MAX_SOURCE_BYTES = 2 * 1024 * 1024
_ROOT_OUTPUTS = {"modus", ".modus", *PROJECT_OUTPUT_DIRS}
_SKIP_FILES = {
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
    ".DS_Store",
}


# 忽略规则随目录继承并按顺序应用，不能把子目录规则提升为全局规则。
# 读取回调复用已校验的完整字节，避免选择后再次读出不同内容。
# 大小、编码和常规文件约束在纳入清单前检查，坏输入不会进入解析器。
def source_files(
    root: Path,
    *,
    extra_excludes: Sequence[str] = (),
    on_read: Callable[[Path, bytes], None] | None = None,
) -> list[Path]:
    """Walk *root* and return every analyzable source file, sorted.

    Selection honours ``.gitignore``/``.modusignore`` rules (directory-scoped,
    ordered), skips symlinks, secrets, lockfiles, binaries, and files larger
    than ``MAX_SOURCE_BYTES``. ``on_read`` observes the head bytes of each
    accepted file, letting callers hash content in the same pass.
    """
    root = root.resolve()
    root_rules = [(root, value) for value in extra_excludes]
    rules_by_directory: dict[Path, list[tuple[Path, str]]] = {}
    result = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        rules = list(rules_by_directory.get(base.parent, ()))
        for name in (".gitignore", ".modusignore"):
            ignore = base / name
            if ignore.is_file() and not ignore.is_symlink():
                for line in ignore.read_text(encoding="utf-8", errors="replace").splitlines():
                    parsed = parse_gitignore_line(line)
                    if parsed:
                        rules.append((base, parsed))
        if base == root:
            rules.extend(root_rules)
        rules_by_directory[base] = rules
        ignored: dict[Path, bool] = {}
        dirs[:] = sorted(
            name
            for name in dirs
            if not (
                (base / name).is_symlink()
                or is_noise_directory(name, base)
                or (base == root and name in _ROOT_OUTPUTS)
                or is_sensitive(base / name / "_")
                or is_ignored(base / name, root, rules, _cache=ignored)
            )
        )
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(root)
            if (
                path.is_symlink()
                or name in _SKIP_FILES
                or name.endswith((".lock", ".min.js", ".map"))
                or relative.as_posix() in MANAGED_ROOT_FILES
                or is_sensitive(relative)
                or is_ignored(path, root, rules, _cache=ignored)
            ):
                continue
            try:
                if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
                    continue
                head = read_bound_regular_bytes(root, path, max_bytes=MAX_SOURCE_BYTES)
                if b"\0" in head:
                    continue
                head.decode("utf-8")
            except (OSError, UnicodeError, BoundKnowledgeReadError):
                continue
            if on_read is not None:
                on_read(path, head)
            result.append(path)
    return sorted(result)


__all__ = ["MAX_SOURCE_BYTES", "source_files"]
