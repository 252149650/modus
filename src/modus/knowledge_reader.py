"""知识读取：POSIX 绑定目录描述符，Windows 核验路径快照与文件身份。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

from .atomic_writes import _is_link_or_reparse, _portable_parent_snapshot

_READ_ONLY_FLAGS: Final = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)


# kind 是供调用者分类恢复的稳定原因，path 保留定位对象。
# 调用者可将 missing 当作可创建状态，但不能把 unsafe 当作不存在。
class BoundKnowledgeReadError(ValueError):
    """A knowledge path could not be read without following links or special files."""

    def __init__(self, kind: str, path: Path, message: str) -> None:
        self.kind = kind
        self.path = path
        super().__init__(message)


def _supports_bound_reads() -> bool:
    """按实际能力选择后端，不能把缺少 openat 的平台当成损坏文件。"""
    return (
        all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
        and {
            os.open,
            os.stat,
        }
        <= os.supports_dir_fd
    )


# 仅由已确认具备 openat 能力的后端调用；能力变化时仍拒绝不安全操作。
def _directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise BoundKnowledgeReadError(
            "unsafe-parent",
            Path("."),
            "绑定知识读取需要 O_NOFOLLOW 与 O_DIRECTORY",
        )
    return _READ_ONLY_FLAGS | no_follow | directory


# 只规范字符串关系，不 resolve 目标，避免校验本身跟随软链。
# 零字节预算合法，可用于验证文件为空；布尔值不作为数字预算接受。
def _validate_read_request(
    root: Path,
    target: Path,
    max_bytes: int | None,
) -> tuple[Path, Path]:
    """Normalize a read request without resolving its target path."""

    root = Path(root)
    target = Path(target)
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0
    ):
        raise ValueError("max_bytes 必须为非负整数或 None")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise BoundKnowledgeReadError(
            "unsafe-parent", target, f"知识路径越出项目根目录： {target}"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BoundKnowledgeReadError("unsafe-parent", target, f"知识路径不规范： {target}")
    return target, relative


# 每一级都相对已绑定描述符打开，后续名称替换不会改变已持有父目录。
# 所有权通过 transferred 显式交给调用者，失败分支负责关闭当前描述符。
def _open_bound_parent(root: Path, relative: Path, target: Path) -> int:
    """Open every parent component through a retained no-follow descriptor."""

    descriptor: int | None = None
    transferred = False
    try:
        descriptor = os.open(root, _directory_flags())
        for part in relative.parts[:-1]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        transferred = True
        return descriptor
    except FileNotFoundError as exc:
        raise BoundKnowledgeReadError(
            "missing", target, f"知识父路径或文件缺失： {target}"
        ) from exc
    except BoundKnowledgeReadError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise BoundKnowledgeReadError(
            "unsafe-parent", target, f"知识父路径不安全： {target}: {exc}"
        ) from exc
    finally:
        if descriptor is not None and not transferred:
            try:
                os.close(descriptor)
            except OSError:
                pass


# O_NONBLOCK 避免特殊文件在类型确认前阻塞；最终只接纳普通文件。
# 类型检查针对打开的描述符，不能只相信打开之前的路径 stat。
def _open_bound_file(parent_fd: int, relative: Path, target: Path) -> tuple[int, os.stat_result]:
    """Open and validate the final path component as a regular file."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_block = getattr(os, "O_NONBLOCK", None)
    if no_follow is None or non_block is None:
        raise BoundKnowledgeReadError(
            "unsafe-file",
            target,
            "绑定知识读取需要 O_NOFOLLOW 与 O_NONBLOCK",
        )
    descriptor: int | None = None
    transferred = False
    try:
        descriptor = os.open(
            relative.name,
            _READ_ONLY_FLAGS | no_follow | non_block,
            dir_fd=parent_fd,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BoundKnowledgeReadError("unsafe-file", target, f"知识条目不是常规文件： {target}")
        transferred = True
        return descriptor, info
    except FileNotFoundError as exc:
        raise BoundKnowledgeReadError("missing", target, f"知识文件缺失： {target}") from exc
    except BoundKnowledgeReadError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise BoundKnowledgeReadError(
            "unsafe-file", target, f"知识文件不安全： {target}: {exc}"
        ) from exc
    finally:
        if descriptor is not None and not transferred:
            try:
                os.close(descriptor)
            except OSError:
                pass


# 预算多读一个字节以发现增长，预先检查 st_size 不能替代读取上限。
# fdopen 接管描述符后由上下文关闭，转换失败则由本函数清理。
# 读取结束比较身份、大小和修改时间，防止把观察期间变化的字节当作快照。
def _read_stable_bytes(
    descriptor: int,
    observed: os.stat_result,
    target: Path,
    max_bytes: int | None,
) -> bytes:
    """Read one opened file and reject size or identity changes during the read."""

    if max_bytes is not None and observed.st_size > max_bytes:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise BoundKnowledgeReadError(
            "oversized-file",
            target,
            f"知识文件超过字节预算 {max_bytes}: {target}",
        )
    try:
        handle = os.fdopen(descriptor, "rb")
    except (OSError, TypeError, ValueError) as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise BoundKnowledgeReadError(
            "unsafe-file", target, f"知识文件不安全： {target}: {exc}"
        ) from exc
    try:
        with handle:
            raw = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
            if max_bytes is not None and len(raw) > max_bytes:
                raise BoundKnowledgeReadError(
                    "oversized-file", target, f"文件增长并超过字节预算： {target}"
                )
            final = os.fstat(handle.fileno())
    except BoundKnowledgeReadError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise BoundKnowledgeReadError(
            "unsafe-file", target, f"知识文件不安全： {target}: {exc}"
        ) from exc
    if (
        (final.st_dev, final.st_ino) != (observed.st_dev, observed.st_ino)
        or final.st_size != observed.st_size
        or final.st_mtime_ns != observed.st_mtime_ns
    ):
        raise BoundKnowledgeReadError(
            "changed-file",
            target,
            f"知识文件在绑定读取过程中发生变化： {target}",
        )
    return raw


def _portable_read_parent(target: Path) -> tuple[tuple[int, int], ...]:
    try:
        return _portable_parent_snapshot(target.parent)
    except FileNotFoundError as exc:
        raise BoundKnowledgeReadError("missing", target, f"知识父路径缺失： {target}") from exc
    except OSError as exc:
        raise BoundKnowledgeReadError(
            "unsafe-parent", target, f"知识父路径不安全： {target}: {exc}"
        ) from exc


def _portable_read_stat(target: Path) -> os.stat_result:
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise BoundKnowledgeReadError("missing", target, f"知识文件缺失： {target}") from exc
    except OSError as exc:
        raise BoundKnowledgeReadError(
            "unsafe-file", target, f"知识文件不安全： {target}: {exc}"
        ) from exc
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise BoundKnowledgeReadError("unsafe-file", target, f"知识条目不是常规文件： {target}")
    return info


def _read_portable_regular_bytes(target: Path, max_bytes: int | None) -> bytes:
    """拒绝软链及重解析点，并在读取前后核验所有父目录和目标身份。

    无 openat 的平台使用与原子写入相同的快照边界；可发现观察到的路径替换，
    但不宣称能阻止外部进程在两次检查之间交换再恢复整个目录树。
    """
    target = Path(os.path.abspath(target))
    parents = _portable_read_parent(target)
    named = _portable_read_stat(target)
    flags = (
        _READ_ONLY_FLAGS
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError as exc:
        raise BoundKnowledgeReadError("missing", target, f"知识文件缺失： {target}") from exc
    except OSError as exc:
        raise BoundKnowledgeReadError(
            "unsafe-file", target, f"知识文件不安全： {target}: {exc}"
        ) from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or (
            named.st_dev,
            named.st_ino,
        ) != (observed.st_dev, observed.st_ino):
            raise BoundKnowledgeReadError(
                "changed-file", target, f"知识文件在打开时发生变化： {target}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    # _read_stable_bytes 接管并关闭文件描述符，包括超预算和读取失败分支。
    raw = _read_stable_bytes(descriptor, observed, target, max_bytes)
    final = _portable_read_stat(target)
    if parents != _portable_read_parent(target) or (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) != (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns):
        raise BoundKnowledgeReadError(
            "changed-file", target, f"知识路径在读取时发生变化： {target}"
        )
    return raw


# 父目录与文件描述符生命周期分离，文件读完仍需关闭保留的目录句柄。
# 此接口只提供安全读取，不授予后续写入或删除的所有权。
def read_bound_regular_bytes(root: Path, target: Path, *, max_bytes: int | None = None) -> bytes:
    """读取项目内常规文件，按平台核验描述符链或路径快照。"""
    root = Path(root)
    target, relative = _validate_read_request(root, target, max_bytes)
    if not _supports_bound_reads():
        return _read_portable_regular_bytes(target, max_bytes)
    directory_fd = _open_bound_parent(root, relative, target)
    try:
        file_fd, observed = _open_bound_file(directory_fd, relative, target)
        return _read_stable_bytes(file_fd, observed, target, max_bytes)
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


# 严格 UTF-8 解码，不能用替换字符静默改变具有权威性的知识内容。
def read_bound_regular_text(root: Path, target: Path, *, max_bytes: int | None = None) -> str:
    """严格解码安全读取到的 UTF-8 字节，保留原始换行和摘要语义。"""
    raw = read_bound_regular_bytes(root, target, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise BoundKnowledgeReadError(
            "invalid-file", Path(target), f"知识文件不是有效 UTF-8： {target}"
        ) from exc


__all__ = [
    "BoundKnowledgeReadError",
    "read_bound_regular_bytes",
    "read_bound_regular_text",
]
