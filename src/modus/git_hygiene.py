"""Transactional Git-index hygiene for disposable Modus project state.

The repository owns ``.gitignore``; Modus owns only one marker-delimited block
inside it.  Every operation therefore parses before writing, refuses malformed
or user-authored block content, and snapshots index entries before an installer
changes them.  Git diagnostics are deliberately best-effort, while mutations
remain explicit and reversible by the caller.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, TypeAlias

from .artifacts_io import atomic_write_text, file_lock
from .paths import ModusPaths

GitHygieneReport: TypeAlias = dict[str, Any]
GitIndexSnapshot: TypeAlias = dict[str, Any]

BEGIN: Final = "# >>> Modus generated local state >>>"
END: Final = "# <<< Modus generated local state <<<"
_MANAGED_RULE_RE: Final = re.compile(r"^/(?:.+/)?modus/\.cache/$")


# Git 命令使用固定可执行文件与参数数组，调用失败转换为统一进程结果。
def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603 - fixed git executable and argv
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    # Hygiene is best-effort at every cache write boundary.  In particular,
    # callers may provide a constrained subprocess shim while launching a
    # background worker; diagnostics must never make that worker fail.
    # NOCA:broad-except(Git 辅助操作的失败统一返回非零结果及原因，由调用方决定阻断)
    except Exception as exc:  # noqa: BLE001 - nonessential Git diagnostic
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def git_root(root: str | Path) -> Path | None:
    """Return the enclosing Git worktree root, or ``None`` outside Git."""
    project = Path(root).resolve()
    result = _git(project, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _relative_state_dirs(paths: ModusPaths, repo_root: Path) -> tuple[str, ...]:
    try:
        data_rel = paths.data_dir.resolve().relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError("Modus 项目根目录必须位于 Git 工作树内") from exc
    return (f"/{data_rel}/.cache/",)


def _block(rules: tuple[str, ...]) -> str:
    return "\n".join((BEGIN, *rules, END)) + "\n"


def _unmerged_gitignore(repo_root: Path) -> bool:
    result = _git(repo_root, "diff", "--name-only", "--diff-filter=U", "--", ".gitignore")
    return result.returncode == 0 and bool(result.stdout.strip())


# 只识别完整成对的受管标记，损坏区块不能被静默重写。
def _parse_block(text: str) -> tuple[tuple[int, int] | None, str | None]:
    begins = [idx for idx, line in enumerate(text.splitlines()) if line == BEGIN]
    ends = [idx for idx, line in enumerate(text.splitlines()) if line == END]
    if not begins and not ends:
        return None, None
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return None, "Modus 本地状态受管区块格式损坏"
    return (begins[0], ends[0]), None


# 共享忽略文件保留非受管正文和已有其他项目规则。
def _upsert_block(text: str, rules: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Return ``(updated, error)`` without rewriting unknown managed content."""
    bounds, error = _parse_block(text)
    if error:
        return None, error
    if bounds is None:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return prefix + _block(rules), None

    lines = text.splitlines(keepends=True)
    start, end = bounds
    contained = [line.rstrip("\r\n") for line in lines[start + 1 : end] if line.strip()]
    # A Git worktree can contain several independently initialized Modus
    # projects. Their root-relative rules share one bounded block, so retain
    # only recognisably generated Modus local-state rules and add this root's
    # pair. Any other text remains a hard stop rather than being overwritten.
    if any(_MANAGED_RULE_RE.fullmatch(line) is None for line in contained):
        return None, "Modus 本地状态区块包含用户维护的内容"
    merged_rules = tuple(sorted(set(contained) | set(rules)))
    desired = [BEGIN, *merged_rules, END]
    current = [line.rstrip("\r\n") for line in lines[start : end + 1]]
    if current == desired:
        return text, None
    lines[start : end + 1] = [f"{line}\n" for line in desired]
    return "".join(lines), None


def remove_local_state_ignored(
    paths: ModusPaths,
    *,
    apply: bool = False,
) -> GitHygieneReport:
    """Remove only this project's rules from the shared Modus ``.gitignore`` block."""
    repo_root = git_root(paths.root)
    if repo_root is None:
        return {"status": "noop", "changed": False}
    target = repo_root / ".gitignore"
    if not target.is_file():
        return {"status": "noop", "changed": False}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "changed": False, "message": str(exc)}
    bounds, error = _parse_block(text)
    if error:
        return {"status": "error", "changed": False, "message": error}
    if bounds is None:
        return {"status": "noop", "changed": False}

    rules = set(_relative_state_dirs(paths, repo_root))
    lines = text.splitlines(keepends=True)
    start, end = bounds
    contained = [line.rstrip("\r\n") for line in lines[start + 1 : end] if line.strip()]
    if any(_MANAGED_RULE_RE.fullmatch(line) is None for line in contained):
        return {
            "status": "error",
            "changed": False,
            "message": "Modus 本地状态区块包含用户维护的内容",
        }
    remaining = sorted(set(contained) - rules)
    if set(remaining) == set(contained):
        return {"status": "noop", "changed": False}
    replacement = [f"{line}\n" for line in (BEGIN, *remaining, END)] if remaining else []
    lines[start : end + 1] = replacement
    updated = "".join(lines)
    if apply:
        if updated.strip():
            atomic_write_text(target, updated)
        else:
            target.unlink(missing_ok=True)
    return {"status": "updated" if apply else "dry-run", "changed": True}


def _hygiene_lock_path(repo_root: Path) -> Path:
    """Return one sandbox-writable lock shared by a Git worktree.

    Git metadata is commonly read-only in coding-agent sandboxes, so a lock
    under ``.git`` turns otherwise harmless cache hygiene into a hard failure.
    The resolved worktree identity keeps nested Modus projects coordinated while
    the per-user temporary root keeps the lock outside both Git and project data.
    """
    identity = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    if hasattr(os, "getuid"):
        user_key = str(os.getuid())
    else:  # pragma: no cover - Windows compatibility
        user_key = hashlib.sha256(str(Path.home()).encode("utf-8")).hexdigest()[:12]
    temp_root = Path(tempfile.gettempdir()).resolve()
    return temp_root / f"modus-{user_key}" / "git-hygiene" / f"{identity}.lock"


def _tracked_paths(repo_root: Path, rules: tuple[str, ...]) -> list[str]:
    prefixes = [rule.strip("/") for rule in rules]
    result = _git(repo_root, "ls-files", "-z", "--", *prefixes)
    if result.returncode != 0:
        return []
    return sorted(item for item in result.stdout.split("\0") if item)


def _ignored(repo_root: Path, path: str) -> bool:
    return _git(repo_root, "check-ignore", "--no-index", "-q", "--", path).returncode == 0


# 预检只判断能否安全修复，安装事务开始前不修改工作区。
def preflight_local_state_hygiene(paths: ModusPaths) -> GitHygieneReport:
    """Validate that init can repair Git hygiene without changing the worktree."""
    repo_root = git_root(paths.root)
    if repo_root is None:
        return {"status": "not_git_repo", "message": "项目不在 Git 工作树中"}
    rules = _relative_state_dirs(paths, repo_root)
    target = repo_root / ".gitignore"
    if target.is_symlink():
        return {
            "status": "blocked",
            "message": ".gitignore 是符号链接",
            "gitRoot": str(repo_root),
            "gitignore": ".gitignore",
            "rules": list(rules),
        }
    if _unmerged_gitignore(repo_root):
        return {
            "status": "blocked",
            "message": ".gitignore 存在未解决的 Git 冲突",
            "gitRoot": str(repo_root),
            "gitignore": ".gitignore",
            "rules": list(rules),
        }
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
    except OSError as exc:
        return {
            "status": "blocked",
            "message": f"无法读取 .gitignore： {exc}",
            "gitRoot": str(repo_root),
            "gitignore": ".gitignore",
            "rules": list(rules),
        }
    _updated, error = _upsert_block(existing, rules)
    if error:
        return {
            "status": "blocked",
            "message": error,
            "gitRoot": str(repo_root),
            "gitignore": ".gitignore",
            "rules": list(rules),
        }
    return {
        "status": "ready",
        "message": "可在事务内应用 Git 本地状态隔离规则",
        "gitRoot": str(repo_root),
        "gitignore": ".gitignore",
        "rules": list(rules),
    }


def _git_bytes(
    root: Path,
    *args: str,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # nosec B603 - fixed git executable and argv
            ["git", "-C", str(root), *args],
            input=input_bytes,
            capture_output=True,
            timeout=20,
        )
    # NOCA:broad-except(索引恢复失败必须返回诊断，由事务层聚合并核验回滚)
    except Exception as exc:  # noqa: BLE001 - rollback diagnostics must be returned
        return subprocess.CompletedProcess(
            ["git", *args],
            1,
            b"",
            str(exc).encode("utf-8", errors="replace"),
        )


# 索引快照保留原始条目，回滚恢复暂存状态而非仅恢复磁盘文件。
def snapshot_local_state_index(paths: ModusPaths) -> GitIndexSnapshot:
    """Capture exact index entries that init's hygiene step may remove."""
    repo_root = git_root(paths.root)
    if repo_root is None:
        return {"status": "not_git_repo", "indexInfo": b"", "rules": []}
    rules = _relative_state_dirs(paths, repo_root)
    result = _git_bytes(
        repo_root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        *(rule.strip("/") for rule in rules),
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(message or "无法创建 Modus 本地状态 Git 索引快照")
    return {
        "status": "ok",
        "gitRoot": str(repo_root),
        "rules": list(rules),
        "indexInfo": result.stdout,
    }


# 按快照恢复受影响索引项，避免覆盖无关的用户暂存修改。
def restore_local_state_index(snapshot: Mapping[str, Any]) -> None:
    """Restore an index snapshot captured by :func:`snapshot_local_state_index`."""
    if snapshot.get("status") == "not_git_repo":
        return
    repo_root = Path(snapshot["gitRoot"])
    rules = [str(rule).strip("/") for rule in snapshot.get("rules", [])]
    removed = _git_bytes(
        repo_root,
        "rm",
        "-r",
        "--cached",
        "--force",
        "--ignore-unmatch",
        "--",
        *rules,
    )
    if removed.returncode != 0:
        message = removed.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(message or "无法清理 Modus 本地状态的 Git 索引项")
    index_info = snapshot.get("indexInfo", b"")
    if index_info:
        restored = _git_bytes(
            repo_root,
            "update-index",
            "-z",
            "--index-info",
            input_bytes=index_info,
        )
        if restored.returncode != 0:
            message = restored.stderr.decode("utf-8", errors="replace").strip()
            raise OSError(message or "无法恢复 Modus 本地状态的 Git 索引项")
    current = _git_bytes(
        repo_root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        *rules,
    )
    if current.returncode != 0 or current.stdout != index_info:
        raise OSError("恢复后的 Git 索引与初始化前快照不一致")


def ensure_local_state_ignored(paths: ModusPaths) -> GitHygieneReport:
    """Idempotently add precise local-state ignore rules to the Git root.

    A malformed block is reported instead of being overwritten, because `.gitignore`
    belongs to the repository rather than Modus' managed manifest.
    """
    repo_root = git_root(paths.root)
    if repo_root is None:
        return {"status": "not_git_repo", "message": "项目不在 Git 工作树中"}
    rules = _relative_state_dirs(paths, repo_root)
    target = repo_root / ".gitignore"
    lock_path = _hygiene_lock_path(repo_root)
    with file_lock(lock_path):
        if _unmerged_gitignore(repo_root):
            return {
                "status": "blocked",
                "message": ".gitignore 存在未解决的 Git 冲突",
                "gitignore": ".gitignore",
                "rules": list(rules),
            }
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
        except OSError as exc:
            return {
                "status": "blocked",
                "message": f"无法读取 .gitignore： {exc}",
                "gitignore": ".gitignore",
                "rules": list(rules),
            }
        updated, error = _upsert_block(existing, rules)
        if error:
            return {
                "status": "blocked",
                "message": error,
                "gitignore": ".gitignore",
                "rules": list(rules),
            }
        assert updated is not None
        status = "ok"
        if updated != existing:
            atomic_write_text(target, updated)
            status = "updated"
        ignored = {rule: _ignored(repo_root, rule.strip("/") + "/.modus-probe") for rule in rules}
        if not all(ignored.values()):
            return {
                "status": "blocked",
                "message": "Git 未应用 Modus 本地状态忽略规则",
                "gitignore": ".gitignore",
                "rules": list(rules),
                "ignored": ignored,
            }
        return {
            "status": status,
            "message": "Git 已忽略 Modus 本地状态",
            "gitRoot": str(repo_root),
            "gitignore": ".gitignore",
            "rules": list(rules),
            "ignored": ignored,
            "trackedLocalState": _tracked_paths(repo_root, rules),
        }


# 工作区尚未建立外层 Git 仓库时，也能准备自己的本地状态忽略规则。
def ensure_workspace_local_state_ignored(paths: ModusPaths) -> GitHygieneReport:
    """Write the workspace-root managed block even before an umbrella Git repo exists."""
    target = paths.root / ".gitignore"
    if target.is_symlink():
        return {"status": "blocked", "message": "workspace .gitignore 是符号链接"}
    rules = ("/modus/.cache/",)
    lock_path = _hygiene_lock_path(paths.root)
    with file_lock(lock_path):
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
        except OSError as exc:
            return {"status": "blocked", "message": f"无法读取工作区 .gitignore： {exc}"}
        updated, error = _upsert_block(existing, rules)
        if error:
            return {"status": "blocked", "message": error, "rules": list(rules)}
        assert updated is not None
        if updated != existing:
            atomic_write_text(target, updated)
            status = "updated"
        else:
            status = "ok"
    return {
        "status": status,
        "message": "工作区已有 Modus 本地状态忽略区块",
        "gitignore": ".gitignore",
        "rules": list(rules),
    }


# 只从索引移除本地状态，不删除磁盘缓存或用户知识内容。
def untrack_local_state(paths: ModusPaths) -> GitHygieneReport:
    """Remove tracked Modus local state from the index without deleting bytes."""
    report = ensure_local_state_ignored(paths)
    if report.get("status") in {"blocked", "not_git_repo"}:
        return report
    repo_root = Path(report["gitRoot"])
    rules = tuple(report["rules"])
    tracked = _tracked_paths(repo_root, rules)
    if not tracked:
        return {
            **report,
            "status": "updated" if report.get("status") == "updated" else "ok",
            "untracked": [],
        }
    result = _git(
        repo_root,
        "rm",
        "-r",
        "--cached",
        "--force",
        "--ignore-unmatch",
        "--",
        *[r.strip("/") for r in rules],
    )
    if result.returncode != 0:
        return {
            **report,
            "status": "blocked",
            "message": result.stderr.strip() or "无法从 Git 索引移除本地状态",
        }
    return {**report, "status": "updated", "untracked": tracked, "trackedLocalState": []}


__all__ = [
    "BEGIN",
    "END",
    "GitHygieneReport",
    "GitIndexSnapshot",
    "ensure_local_state_ignored",
    "ensure_workspace_local_state_ignored",
    "git_root",
    "preflight_local_state_hygiene",
    "remove_local_state_ignored",
    "restore_local_state_index",
    "snapshot_local_state_index",
    "untrack_local_state",
]
