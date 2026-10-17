"""Corruption-tolerant loading of Modus-generated artifacts.

Modus-generated artifacts (evidence bundles, configuration, domain metadata,
and disposable caches) are machine-written YAML/JSON. A hand-edit, merge accident, or
interrupted write can leave them malformed. The human review surface is friendly
Markdown/YAML plus commands, so a malformed artifact must surface as a readable,
actionable error -- never a raw parser traceback. Disposable caches instead
self-heal (``tolerant=True``): a corrupt cache is treated as absent and rebuilt.

Two load modes:

* strict (default): malformed content raises :class:`CorruptArtifactError`, whose
  message names the file, the failure, and how to recover. CLI gate commands
  turn this into a clean non-zero exit instead of a traceback.
* tolerant: malformed content returns ``default`` so a regeneration/rebuild path
  can overwrite the bad file instead of crashing on it.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Final

import yaml

from .atomic_writes import (
    AtomicWriteIntegrityError,
    _assert_portable_atomic_parent,
    _ensure_portable_atomic_parent,
    _is_link_or_reparse,
    atomic_create_text,
    atomic_write_chunks,
    atomic_write_text,
)

logger = logging.getLogger(__name__)

_HELD_FILE_LOCK_DEPTHS = threading.local()
_OPEN_FILE_LOCKS: dict[int, object] = {}
_OPEN_FILE_LOCKS_GUARD = threading.Lock()
_FILE_LOCK_REGISTRY_PID = os.getpid()
_LOCK_BIND_ATTEMPTS: Final = 8


def _reset_file_lock_registry_after_pid_change() -> None:
    """Drop copied ownership and close inherited lock descriptors in a fork child."""
    global _FILE_LOCK_REGISTRY_PID, _OPEN_FILE_LOCKS, _OPEN_FILE_LOCKS_GUARD

    inherited = set(_OPEN_FILE_LOCKS.values())
    for descriptor in tuple(_OPEN_FILE_LOCKS):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _OPEN_FILE_LOCKS = {}
    _OPEN_FILE_LOCKS_GUARD = threading.Lock()
    _HELD_FILE_LOCK_DEPTHS.depths = {}
    _HELD_FILE_LOCK_DEPTHS.required_depths = {}
    _HELD_FILE_LOCK_DEPTHS.inherited_tokens = inherited
    _FILE_LOCK_REGISTRY_PID = os.getpid()


def _ensure_file_lock_registry_pid() -> None:
    if _FILE_LOCK_REGISTRY_PID != os.getpid():
        _reset_file_lock_registry_after_pid_change()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_file_lock_registry_after_pid_change)


class CorruptArtifactError(Exception):
    """A Modus-generated artifact exists but could not be parsed.

    Carries enough structured context for the CLI to print a friendly, actionable
    message (which file, what kind, how to recover).
    """

    def __init__(self, path: str | Path, kind: str, detail: str, hint: str | None = None) -> None:
        self.path = str(path)
        self.kind = kind
        self.detail = detail
        self.hint = hint
        message = f"{kind} 格式损坏： {self.path}\n  -> {detail}"
        if hint:
            message += f"\n  -> {hint}"
        super().__init__(message)


class FileLockIntegrityError(ValueError):
    """A writer lock path could not be bound without following links."""


class RequiredFileLockUnavailable(RuntimeError):
    """A correctness-critical writer lock has no safe platform backend."""


def _detail(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]


def safe_load_yaml(
    path: str | Path,
    *,
    kind: str,
    default: Any = None,
    tolerant: bool = False,
    hint: str | None = None,
) -> Any:
    """Load a YAML artifact, mapping malformed content to a friendly outcome.

    Returns ``default`` when the file is missing or empty. On malformed YAML:
    raise :class:`CorruptArtifactError` (strict) or return ``default`` (tolerant).
    """
    p = Path(path)
    if not p.is_file():
        return default
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        if tolerant:
            return default
        raise CorruptArtifactError(p, kind, _detail(exc), hint) from exc
    return default if data is None else data


def _lock_directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise FileLockIntegrityError("安全写锁需要 O_NOFOLLOW 与 O_DIRECTORY")
    return os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)


def _absolute_lock_path(lock_path: str | Path) -> Path:
    """Return a lexical absolute path without resolving any filesystem link."""
    return Path(os.path.abspath(os.fspath(lock_path)))


def _assert_lock_parent_binding(parent: Path, descriptor: int) -> None:
    """Verify every named ancestor and the retained parent descriptor agree."""
    anchor = Path(parent.anchor)
    current = anchor
    try:
        info = os.stat(anchor, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise FileLockIntegrityError(f"写锁锚点不是目录： {anchor}")
        for part in parent.parts[1:]:
            current = current / part
            info = os.stat(current, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise FileLockIntegrityError(f"写锁父路径不是实际目录： {current}")
        opened = os.fstat(descriptor)
    except FileLockIntegrityError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise FileLockIntegrityError(f"写锁父路径不安全或不可用： {current}: {exc}") from exc
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        raise FileLockIntegrityError(f"写锁父路径在打开后发生变化： {parent}")


def _open_lock_parent(parent: Path) -> int:
    """Create/open ``parent`` one component at a time without following links."""
    flags = _lock_directory_flags()
    descriptor: int | None = None
    try:
        descriptor = os.open(parent.anchor, flags)
        for part in parent.parts[1:]:
            if part in {"", ".", ".."}:
                raise FileLockIntegrityError(f"写锁父路径不规范： {parent}")
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _assert_lock_parent_binding(parent, descriptor)
        return descriptor
    except FileLockIntegrityError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise FileLockIntegrityError(f"写锁父路径不安全或不可用： {parent}: {exc}") from exc


def _assert_lock_file_binding(parent_descriptor: int, name: str, descriptor: int) -> None:
    """Require the lock name to remain bound to the opened regular-file inode."""
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except (OSError, TypeError, NotImplementedError) as exc:
        raise FileLockIntegrityError(f"写锁文件不安全或不可用： {name}: {exc}") from exc
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
        raise FileLockIntegrityError(f"写锁必须为常规文件： {name}")
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise FileLockIntegrityError(f"写锁路径在打开后发生变化： {name}")


def _open_lock_file(parent_descriptor: int, name: str) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_block = getattr(os, "O_NONBLOCK", None)
    if no_follow is None or non_block is None:
        raise FileLockIntegrityError("安全写锁需要 O_NOFOLLOW 与 O_NONBLOCK")
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | no_follow | non_block | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
    except (OSError, TypeError, NotImplementedError) as exc:
        raise FileLockIntegrityError(f"写锁文件不安全或不可用： {name}: {exc}") from exc
    try:
        _assert_lock_file_binding(parent_descriptor, name, descriptor)
    # NOCA:broad-except(校验失败时先关闭已打开的锁描述符，再原样抛出异常)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _register_open_file_lock(descriptor: int) -> object:
    _ensure_file_lock_registry_pid()
    token = object()
    with _OPEN_FILE_LOCKS_GUARD:
        _OPEN_FILE_LOCKS[descriptor] = token
    return token


def _unregister_open_file_lock(descriptor: int, token: object) -> None:
    _ensure_file_lock_registry_pid()
    with _OPEN_FILE_LOCKS_GUARD:
        if _OPEN_FILE_LOCKS.get(descriptor) is token:
            _OPEN_FILE_LOCKS.pop(descriptor, None)


def _file_lock_token_was_inherited(token: object | None) -> bool:
    if token is None:
        return False
    _ensure_file_lock_registry_pid()
    inherited: set[object] = getattr(_HELD_FILE_LOCK_DEPTHS, "inherited_tokens", set())
    return token in inherited


@dataclass(slots=True)
class _PosixLockHandle:
    """Own every descriptor and registry token for one POSIX writer lock."""

    parent_fd: int | None = None
    lock_fd: int | None = None
    parent_token: object | None = None
    lock_token: object | None = None
    locked: bool = False

    def close(self, fcntl_module: Any, *, preserve_inherited: bool) -> None:
        """Release registrations and descriptors in dependency order."""

        try:
            if self.lock_fd is not None:
                inherited = preserve_inherited and _file_lock_token_was_inherited(self.lock_token)
                if self.lock_token is not None:
                    _unregister_open_file_lock(self.lock_fd, self.lock_token)
                if not inherited:
                    try:
                        if self.locked:
                            fcntl_module.flock(self.lock_fd, fcntl_module.LOCK_UN)
                    finally:
                        os.close(self.lock_fd)
        finally:
            if self.parent_fd is not None:
                inherited = preserve_inherited and _file_lock_token_was_inherited(self.parent_token)
                if self.parent_token is not None:
                    _unregister_open_file_lock(self.parent_fd, self.parent_token)
                if not inherited:
                    os.close(self.parent_fd)


def _acquire_posix_lock(path: Path, fcntl_module: Any) -> _PosixLockHandle:
    """Safely bind and acquire one logical lock, retrying concurrent first use."""

    for attempt in range(_LOCK_BIND_ATTEMPTS):
        handle = _PosixLockHandle()
        try:
            handle.parent_fd = _open_lock_parent(path.parent)
            handle.parent_token = _register_open_file_lock(handle.parent_fd)
            handle.lock_fd = _open_lock_file(handle.parent_fd, path.name)
            handle.lock_token = _register_open_file_lock(handle.lock_fd)
            fcntl_module.flock(handle.lock_fd, fcntl_module.LOCK_EX)
            handle.locked = True
            _assert_lock_parent_binding(path.parent, handle.parent_fd)
            _assert_lock_file_binding(handle.parent_fd, path.name, handle.lock_fd)
            return handle
        except FileLockIntegrityError:
            handle.close(fcntl_module, preserve_inherited=False)
            if attempt == _LOCK_BIND_ATTEMPTS - 1:
                raise
        except BaseException:
            handle.close(fcntl_module, preserve_inherited=False)
            raise
    raise AssertionError("锁获取进入不应出现的状态")


@contextmanager
def file_lock(lock_path: str | Path) -> Iterator[None]:
    """Advisory exclusive lock around a critical section (POSIX ``flock``).

    Serializes concurrent writers (e.g. two initialization-analysis builds, or two
    ``promote`` runs feeding the same domain) so their read-modify-write never
    interleaves. Degrades to a no-op where ``fcntl`` is unavailable (non-POSIX):
    best-effort, never blocks the workflow. The lock file lives wherever the
    caller points it -- by convention under the disposable cache, never inside an
    output tree the lock protects.
    """
    _ensure_file_lock_registry_pid()
    try:
        import fcntl
    # NOCA:broad-except(可选锁后端不可用时按既有契约降级，事务写入使用必需锁入口)
    except Exception:  # noqa: BLE001 - non-POSIX: best-effort, no locking
        yield
        return

    # Directory creation and the first lock open are intentionally outside any
    # pre-existing mutex. Concurrent first-use writers can replace a just-made
    # path component between openat/fstat checks. Retry the complete safe bind;
    # every attempt still uses O_NOFOLLOW and never relaxes the symlink policy.
    path = _absolute_lock_path(lock_path)
    handle = _acquire_posix_lock(path, fcntl)
    try:
        yield
        assert handle.parent_fd is not None and handle.lock_fd is not None
        _assert_lock_parent_binding(path.parent, handle.parent_fd)
        _assert_lock_file_binding(handle.parent_fd, path.name, handle.lock_fd)
    finally:
        handle.close(fcntl, preserve_inherited=True)


def _assert_portable_lock_binding(
    path: Path,
    descriptor: int,
    parent_snapshot: tuple[tuple[int, int], ...] | None = None,
) -> None:
    """Validate the regular-file binding used by the native Windows backend."""
    try:
        _assert_portable_atomic_parent(path.parent, parent_snapshot)
        opened = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
    except FileLockIntegrityError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise FileLockIntegrityError(f"写锁不安全或不可用： {path}: {exc}") from exc
    if _is_link_or_reparse(named) or not stat.S_ISREG(named.st_mode):
        raise FileLockIntegrityError(f"写锁必须为常规文件： {path}")
    if not stat.S_ISREG(opened.st_mode) or (
        opened.st_dev,
        opened.st_ino,
    ) != (named.st_dev, named.st_ino):
        raise FileLockIntegrityError(f"写锁路径在打开后发生变化： {path}")


@contextmanager
def _windows_required_file_lock(lock_path: str | Path, msvcrt_module: Any) -> Iterator[None]:
    """Acquire a mandatory one-byte Windows file lock without shell tooling."""
    path = _absolute_lock_path(lock_path)
    try:
        parents = _ensure_portable_atomic_parent(path.parent)
    except OSError as exc:
        raise FileLockIntegrityError(f"写锁父路径不安全或不可用： {path}: {exc}") from exc
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        _assert_portable_lock_binding(path, descriptor, parents)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt_module.locking(descriptor, msvcrt_module.LK_LOCK, 1)
        locked = True
        _assert_portable_lock_binding(path, descriptor, parents)
        yield
        _assert_portable_lock_binding(path, descriptor, parents)
    finally:
        if locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt_module.locking(descriptor, msvcrt_module.LK_UNLCK, 1)
        os.close(descriptor)


@contextmanager
def required_file_lock(lock_path: str | Path) -> Iterator[None]:
    """Acquire a cross-platform writer lock or fail closed.

    Use this for state transitions whose correctness depends on mutual exclusion
    (for example knowledge and install writes). Read-only callers may continue to use
    :func:`file_lock`'s best-effort behavior, but transactional writers must not
    silently continue without a supported lock backend.
    """
    try:
        # 此处只探测后端；实际句柄仍由 file_lock 管理。
        import_module("fcntl")
    # NOCA:broad-except(后端加载失败时仅切换探测 Windows 锁，不放行无锁写入)
    except Exception:  # noqa: BLE001 - probe the native Windows backend next
        try:
            import msvcrt
        # NOCA:broad-except(两个锁后端均不可用时抛出必需锁异常，保留原因并阻止写入)
        except Exception as exc:  # noqa: BLE001 - fail closed by contract
            raise RequiredFileLockUnavailable(
                "必需写锁需要 POSIX fcntl 或 Windows msvcrt 支持"
            ) from exc
        logger.debug("正在获取必需的 Windows 写锁", extra={"path": str(lock_path)})
        with _windows_required_file_lock(lock_path, msvcrt):
            yield
        return

    logger.debug("正在获取必需的 POSIX 写锁", extra={"path": str(lock_path)})
    with file_lock(lock_path):
        yield


@contextmanager
def required_reentrant_file_lock(lock_path: str | Path) -> Iterator[None]:
    """Acquire one correctness-critical lock per thread and canonical path."""
    identity = _file_lock_identity(lock_path)
    depths = _held_file_lock_depths()
    required_depths = _held_required_file_lock_depths()
    if required_depths.get(identity, 0):
        depths[identity] += 1
        required_depths[identity] += 1
        try:
            yield
        finally:
            depths[identity] -= 1
            required_depths[identity] -= 1
        return

    if depths.get(identity, 0):
        # An advisory lock is correctness-equivalent only on POSIX, where
        # ``file_lock`` necessarily acquired flock. On Windows that same API is
        # deliberately a no-op, so a nested transaction must still acquire
        # the native required backend instead of mistaking thread depth for an
        # operating-system lock.
        try:
            # 只有 POSIX 的 advisory 锁可以提升为必需写锁。
            import_module("fcntl")
        # NOCA:broad-except(可选锁不能证明互斥时继续获取必需锁，不能把线程深度当系统锁)
        except Exception:  # noqa: BLE001 - native required backend acquired below
            pass
        else:
            depths[identity] += 1
            required_depths[identity] = 1
            try:
                yield
            finally:
                depths[identity] -= 1
                required_depths.pop(identity, None)
            return

    with required_file_lock(lock_path):
        previous_depth = depths.get(identity, 0)
        depths[identity] = previous_depth + 1
        required_depths[identity] = 1
        try:
            yield
        finally:
            required_depths.pop(identity, None)
            if previous_depth:
                depths[identity] = previous_depth
            else:
                depths.pop(identity, None)


@contextmanager
def reentrant_file_lock(lock_path: str | Path) -> Iterator[None]:
    """Acquire one advisory writer lock once per current thread and canonical path."""
    identity = _file_lock_identity(lock_path)
    depths = _held_file_lock_depths()
    if depths.get(identity, 0):
        depths[identity] += 1
        try:
            yield
        finally:
            depths[identity] -= 1
        return
    with file_lock(lock_path):
        depths[identity] = 1
        try:
            yield
        finally:
            depths.pop(identity, None)


def _file_lock_identity(lock_path: str | Path) -> str:
    return os.path.normcase(os.fspath(_absolute_lock_path(lock_path)))


def _held_file_lock_depths() -> dict[str, int]:
    _ensure_file_lock_registry_pid()
    depths = getattr(_HELD_FILE_LOCK_DEPTHS, "depths", None)
    if depths is None:
        depths = {}
        _HELD_FILE_LOCK_DEPTHS.depths = depths
    return depths


def _held_required_file_lock_depths() -> dict[str, int]:
    _ensure_file_lock_registry_pid()
    depths = getattr(_HELD_FILE_LOCK_DEPTHS, "required_depths", None)
    if depths is None:
        depths = {}
        _HELD_FILE_LOCK_DEPTHS.required_depths = depths
    return depths


__all__ = [
    "AtomicWriteIntegrityError",
    "CorruptArtifactError",
    "FileLockIntegrityError",
    "RequiredFileLockUnavailable",
    "atomic_create_text",
    "atomic_write_chunks",
    "atomic_write_text",
    "file_lock",
    "reentrant_file_lock",
    "required_file_lock",
    "required_reentrant_file_lock",
    "safe_load_yaml",
]
