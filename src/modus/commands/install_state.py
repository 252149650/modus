"""安装事务的预检、精确快照与回滚核验。

第一笔写入前检查所有目标，失败后恢复字节、权限与目录形态；
恢复不完整时明确列出需要人工检查的路径。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import git_hygiene
from ..paths import ModusPaths

__all__ = [
    "InitConflictError",
    "InitFailedError",
    "InitRollbackError",
    "InstallTransaction",
    "unsafe_target_issue",
    "validate_install_root",
]


class InitConflictError(ValueError):
    """The target cannot be installed safely without a user decision."""

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = tuple(conflicts)
        lines = ["Modus 未完成初始化，未写入任何文件。"]
        if conflicts:
            lines.append("发现以下冲突：")
            lines.extend(f"  - {item}" for item in conflicts)
        lines.append("请移走或修复冲突文件，再运行 `modus init`。")
        super().__init__("\n".join(lines))


class InitFailedError(RuntimeError):
    """Initialization failed but the original project state was restored."""


class InitRollbackError(RuntimeError):
    """Initialization failed and one or more rollback checks also failed."""


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    existed: bool
    data: bytes = b""
    mode: int = 0


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def validate_install_root(root: str | Path) -> ModusPaths:
    """Accept only an existing, non-filesystem-root directory as install target."""
    requested = Path(root).expanduser()
    if not requested.exists():
        raise InitConflictError([f"项目目录不存在： {requested}"])
    if not requested.is_dir():
        raise InitConflictError([f"目标不是目录： {requested}"])
    paths = ModusPaths.for_root(requested)
    if paths.root.parent == paths.root:
        raise InitConflictError(["不能在文件系统根目录初始化 Modus。"])
    return paths


def unsafe_target_issue(root: Path, target: Path) -> str | None:
    """Explain why writing ``target`` would be unsafe, or ``None`` when it is fine.

    Rejects paths escaping ``root`` and any component that is a symlink or
    otherwise not the expected regular file / directory, so a crafted layout
    can never redirect an install write outside the project.
    """
    try:
        relative = target.relative_to(root)
    except ValueError:
        return f"目标路径越出项目根目录： {target}"
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if not _lexists(current):
            continue
        info = os.lstat(current)
        final = index == len(relative.parts) - 1
        if stat.S_ISLNK(info.st_mode):
            return f"路径包含符号链接： {relative.as_posix()}"
        if final:
            if not stat.S_ISREG(info.st_mode):
                return f"目标不是常规文件： {relative.as_posix()}"
        elif not stat.S_ISDIR(info.st_mode):
            return f"父路径不是目录： {relative.as_posix()}"
    return None


class InstallTransaction:
    """Byte-exact snapshots of every planned target, taken before the first write.

    ``rollback()`` restores file contents, modes, and directory shape, then
    re-verifies every restoration; any discrepancy raises
    :class:`InitRollbackError` listing the paths that need human attention.
    """

    def __init__(
        self,
        paths: ModusPaths,
        targets: list[Path],
        index_snapshot: dict[str, Any],
    ) -> None:
        self.paths = paths
        self.index_snapshot = index_snapshot
        self.snapshots: list[_FileSnapshot] = []
        self.existing_dirs: dict[Path, int] = {}
        self.absent_dirs: set[Path] = set()
        for target in targets:
            self.snapshots.append(self._snapshot_file(target))
            if target.is_relative_to(paths.root):
                self._snapshot_parents(target.parent)

    @staticmethod
    def _snapshot_file(path: Path) -> _FileSnapshot:
        if not _lexists(path):
            return _FileSnapshot(path=path, existed=False)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise InitConflictError([f"事务目标不是常规文件： {path}"])
        return _FileSnapshot(
            path=path,
            existed=True,
            data=path.read_bytes(),
            mode=stat.S_IMODE(info.st_mode),
        )

    def _snapshot_parents(self, parent: Path) -> None:
        current = parent
        while current != self.paths.root and current.is_relative_to(self.paths.root):
            if _lexists(current):
                info = os.lstat(current)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise InitConflictError([f"事务父路径不安全： {current}"])
                self.existing_dirs[current] = stat.S_IMODE(info.st_mode)
            else:
                self.absent_dirs.add(current)
            current = current.parent

    def rollback(self) -> None:
        errors: list[str] = []
        for snapshot in self.snapshots:
            try:
                _restore_file_snapshot(snapshot)
            # NOCA:broad-except(单路径恢复失败不能中断其余恢复，最终聚合为回滚不完整错误)
            except Exception as exc:  # noqa: BLE001 - aggregate every rollback failure
                errors.append(f"{snapshot.path}: {exc}")

        for directory, mode in sorted(
            self.existing_dirs.items(), key=lambda item: len(item[0].parts)
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(mode)
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
        for directory in sorted(self.absent_dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError as exc:
                errors.append(f"{directory}: {exc}")

        try:
            git_hygiene.restore_local_state_index(self.index_snapshot)
        # NOCA:broad-except(索引恢复故障加入回滚失败清单，后续继续核验文件)
        except Exception as exc:  # noqa: BLE001 - reported as rollback-incomplete
            errors.append(f"Git 索引： {exc}")

        for snapshot in self.snapshots:
            try:
                if snapshot.existed:
                    if not _lexists(snapshot.path):
                        errors.append(f"{snapshot.path}：恢复后的文件缺失")
                        continue
                    info = os.lstat(snapshot.path)
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or not stat.S_ISREG(info.st_mode)
                        or snapshot.path.read_bytes() != snapshot.data
                    ):
                        errors.append(f"{snapshot.path}：恢复后的内容不匹配")
                elif _lexists(snapshot.path):
                    errors.append(f"{snapshot.path}：新建路径仍然存在")
            except OSError as exc:
                errors.append(f"{snapshot.path}：回滚核验失败： {exc}")
        if errors:
            detail = "\n".join(f"  - {item}" for item in errors)
            raise InitRollbackError(
                "严重错误 rollback-incomplete：安装事务失败，且"
                "回滚未能完整恢复项目。\n"
                "请手动检查以下路径：\n"
                f"{detail}"
            )


def _restore_file_snapshot(snapshot: _FileSnapshot) -> None:
    """只恢复记录的文件形态；调用方聚合异常并在全部恢复后统一核验。"""
    if snapshot.existed:
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        if _lexists(snapshot.path):
            info = os.lstat(snapshot.path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OSError("回滚目标已不再是常规文件")
        snapshot.path.write_bytes(snapshot.data)
        snapshot.path.chmod(snapshot.mode)
    elif _lexists(snapshot.path):
        info = os.lstat(snapshot.path)
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            snapshot.path.unlink()
        else:
            raise OSError("无法按文件删除新增的回滚目标")
