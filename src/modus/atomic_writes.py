"""Contained atomic publication for mutable and immutable text artifacts.

Writers bind each directory and target without following links, then verify that
binding before publishing. POSIX uses descriptor-relative operations; the portable
backend verifies the same ownership through snapshots. A failed attempt removes
only its own temporary file and never silently falls back to an unsafe write.

These primitives provide atomicity for one file. Transaction ordering, locking,
compare-and-swap and rollback remain the caller's responsibility.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterable
from pathlib import Path


class AtomicWriteIntegrityError(OSError):
    """An ``atomic_write_text`` target could not be safely bound or written.

    Raised instead of silently writing through a symlink/special file, or
    instead of completing a replace whose parent directory was swapped out
    from under the write. A well-formed, non-adversarial target never raises
    this -- only a malformed or actively-racing target does.
    """


def _absolute_target_path(path: str | Path) -> Path:
    """Normalize lexically; resolving a symlink here would erase the safety check."""
    return Path(os.path.abspath(os.fspath(path)))


def _atomic_write_temp_name(name: str) -> str:
    """A per-attempt temp name: PID *and* a random nonce.

    PID alone collides across a fork boundary or under PID reuse (two
    concurrent writers can legitimately share a PID). The nonce makes every
    attempt's temp name unique regardless of PID, so 64 same-PID writers (or
    genuinely different processes) never contend for one temp path.
    """
    return f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"


def _reject_unsafe_atomic_write_target(
    parent_descriptor: int | None, name: str, path: Path
) -> None:
    """Fail closed if an existing target is a symlink or special file.

    Checked via ``lstat`` (never following the final component), so this
    guarantee holds even when ``O_NOFOLLOW`` is unavailable for the open calls
    below -- the property does not depend on those flags.
    """
    try:
        info = (
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if parent_descriptor is not None
            else path.lstat()
        )
    except FileNotFoundError:
        return
    except (OSError, TypeError, NotImplementedError) as exc:
        raise AtomicWriteIntegrityError(f"原子写入目标不安全或不可用： {path}: {exc}") from exc
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise AtomicWriteIntegrityError(
            f"原子写入目标必须为常规文件或不存在，不能为符号链接或特殊文件： {path}"
        )


def _open_atomic_child(parent_fd: int, name: str, current: Path, flags: int) -> int:
    """Create/open and inode-bind one directory below an already safe parent."""

    try:
        os.mkdir(name, mode=0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    child = os.open(name, flags, dir_fd=parent_fd)
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(child)
        if (
            _is_link_or_reparse(named)
            or not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise AtomicWriteIntegrityError(f"原子写入父路径不是实际目录： {current}")
        return child
    except BaseException:
        os.close(child)
        raise


def _open_atomic_write_parent(parent: Path) -> int:
    """Create/open every ``parent`` component without following filesystem links.

    A single ``os.open(parent, O_NOFOLLOW | ...)`` call only rejects a symlink
    at the *final* path component -- the kernel still resolves every
    intermediate component through ordinary path resolution, so a symlinked
    grandparent (or higher) would otherwise be silently followed. Opening
    component-by-component with ``dir_fd`` closes that gap. Missing directories
    are created relative to an already-bound parent, avoiding the unsafe
    ``Path.mkdir(parents=True)`` preflight that can traverse an ancestor link.
    """
    open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is not None:
        open_flags |= no_follow
    if hasattr(os, "O_DIRECTORY"):
        open_flags |= os.O_DIRECTORY
    descriptor: int | None = None
    transferred = False
    try:
        descriptor = os.open(parent.anchor, open_flags)
        current = Path(parent.anchor)
        for part in parent.parts[1:]:
            if part in {"", ".", ".."}:
                raise AtomicWriteIntegrityError(f"原子写入父路径不规范： {parent}")
            current = current / part
            child = _open_atomic_child(descriptor, part, current, open_flags)
            os.close(descriptor)
            descriptor = child
        _assert_atomic_write_parent_binding(parent, descriptor)
        transferred = True
        return descriptor
    except AtomicWriteIntegrityError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise AtomicWriteIntegrityError(f"原子写入父路径不安全或不可用： {parent}: {exc}") from exc
    finally:
        if descriptor is not None and not transferred:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _assert_atomic_write_parent_binding(parent: Path, descriptor: int) -> None:
    """Verify every named ancestor and the open parent descriptor agree.

    Mirrors ``file_lock``'s ``_assert_lock_parent_binding``: walks the full
    ancestor chain (not just the immediate parent) with an unresolved
    ``stat``, then compares the innermost component's identity against the
    descriptor actually held open. A parent-directory swap anywhere in the
    chain -- not only the last component -- is therefore detected.
    """
    anchor = Path(parent.anchor)
    current = anchor
    try:
        info = os.stat(anchor, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise AtomicWriteIntegrityError(f"原子写入锚点不是目录： {anchor}")
        for part in parent.parts[1:]:
            current = current / part
            info = os.stat(current, follow_symlinks=False)
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise AtomicWriteIntegrityError(f"原子写入父路径不是实际目录： {current}")
        opened = os.fstat(descriptor)
    except AtomicWriteIntegrityError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise AtomicWriteIntegrityError(f"原子写入父路径不安全或不可用： {current}: {exc}") from exc
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        raise AtomicWriteIntegrityError(f"原子写入父路径在打开后发生变化： {parent}")


def _is_link_or_reparse(info: os.stat_result) -> bool:
    """Recognize POSIX symlinks and Windows reparse-point indirections."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _portable_lstat(path: Path) -> os.stat_result:
    """Return unresolved metadata on both POSIX and native Windows Python."""
    try:
        return os.stat(path, follow_symlinks=False)
    except (TypeError, NotImplementedError):  # pragma: no cover - old Windows Python
        return path.lstat()


def _portable_parent_snapshot(parent: Path) -> tuple[tuple[int, int], ...]:
    """Validate an atomic-write ancestor chain and return stable identities.

    This is the strongest portable fallback available where ``openat`` and
    ``O_DIRECTORY`` are absent (notably native Windows). Reparse points are
    rejected explicitly; callers compare the snapshot again immediately before
    publication to catch ordinary directory replacement races.
    """
    absolute = _absolute_target_path(parent)
    anchor = Path(absolute.anchor)
    if not absolute.anchor:
        raise AtomicWriteIntegrityError(f"原子写入父路径必须为绝对路径： {parent}")
    current = anchor
    identities: list[tuple[int, int]] = []
    try:
        info = _portable_lstat(current)
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise AtomicWriteIntegrityError(f"原子写入父路径不是实际目录： {current}")
        identities.append((info.st_dev, info.st_ino))
        for part in absolute.parts[1:]:
            if part in {"", ".", ".."}:
                raise AtomicWriteIntegrityError(f"原子写入父路径不规范： {absolute}")
            current = current / part
            info = _portable_lstat(current)
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise AtomicWriteIntegrityError(f"原子写入父路径不是实际目录： {current}")
            identities.append((info.st_dev, info.st_ino))
    except (AtomicWriteIntegrityError, FileNotFoundError):
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise AtomicWriteIntegrityError(f"原子写入父路径不安全或不可用： {current}: {exc}") from exc
    return tuple(identities)


def _assert_portable_atomic_parent(
    parent: Path, expected: tuple[tuple[int, int], ...] | None = None
) -> tuple[tuple[int, int], ...]:
    """Validate portable ancestors and optionally compare their identities."""
    try:
        actual = _portable_parent_snapshot(parent)
    except FileNotFoundError as exc:
        raise AtomicWriteIntegrityError(f"原子写入父路径不安全或不可用： {parent}: {exc}") from exc
    if expected is not None and actual != expected:
        raise AtomicWriteIntegrityError(f"原子写入父路径在发布期间发生变化： {parent}")
    return actual


def _ensure_portable_atomic_parent(parent: Path) -> tuple[tuple[int, int], ...]:
    """Create missing fallback directories without traversing a static link."""
    absolute = _absolute_target_path(parent)
    anchor = Path(absolute.anchor)
    if not absolute.anchor:
        raise AtomicWriteIntegrityError(f"原子写入父路径必须为绝对路径： {parent}")
    current = anchor
    try:
        anchor_info = _portable_lstat(anchor)
        if _is_link_or_reparse(anchor_info) or not stat.S_ISDIR(anchor_info.st_mode):
            raise AtomicWriteIntegrityError(f"原子写入锚点不是实际目录： {anchor}")
        for part in absolute.parts[1:]:
            if part in {"", ".", ".."}:
                raise AtomicWriteIntegrityError(f"原子写入父路径不规范： {absolute}")
            current = current / part
            try:
                info = _portable_lstat(current)
            except FileNotFoundError:
                # Re-check every existing ancestor immediately before the
                # path-based mkdir. Native Windows has no dir_fd equivalent,
                # so this is intentionally fail-closed against static links
                # and best-effort against an active swap race.
                _assert_portable_atomic_parent(current.parent)
                try:
                    os.mkdir(current, mode=0o755)
                except FileExistsError:
                    # A concurrent benign writer may have won directory
                    # creation; the unresolved metadata check below decides
                    # whether that winner is safe.
                    pass
                info = _portable_lstat(current)
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise AtomicWriteIntegrityError(f"原子写入父路径不是实际目录： {current}")
    except AtomicWriteIntegrityError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise AtomicWriteIntegrityError(f"原子写入父路径不安全或不可用： {current}: {exc}") from exc
    return _assert_portable_atomic_parent(absolute)


def _atomic_write_posix(parent: Path, name: str, data: bytes | Iterable[bytes], path: Path) -> None:
    """Bind the parent directory, then create-fsync-rename-fsync under it.

    The parent is opened one path component at a time (mirrors ``file_lock``'s
    ``_open_lock_parent``/``_assert_lock_parent_binding``), so a symlink
    anywhere in the ancestor chain -- not just the immediate parent -- is
    rejected; a single multi-component ``os.open(parent, O_NOFOLLOW)`` call
    would only protect the final component. Before the final rename the whole
    chain is re-checked against a fresh, unresolved ``stat``, so a parent-
    directory swap anywhere in that chain between temp-file creation and
    replace is detected and fails closed instead of silently renaming into the
    wrong directory.
    """
    no_follow = getattr(os, "O_NOFOLLOW", None)
    parent_descriptor = _open_atomic_write_parent(parent)
    try:
        parent_stat = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise AtomicWriteIntegrityError(f"原子写入父路径不是目录： {parent}")
        _reject_unsafe_atomic_write_target(parent_descriptor, name, path)

        tmp_name = _atomic_write_temp_name(name)
        create_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | (no_follow or 0)
        )
        fd = os.open(tmp_name, create_flags, 0o600, dir_fd=parent_descriptor)
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in (data,) if isinstance(data, bytes) else data:
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            _assert_atomic_write_parent_binding(parent, parent_descriptor)
            _reject_unsafe_atomic_write_target(parent_descriptor, name, path)

            if os.rename in os.supports_dir_fd:
                os.rename(
                    tmp_name, name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor
                )
            else:  # pragma: no cover - exercised only on platforms without renameat
                os.replace(parent / tmp_name, path)
        except BaseException:
            try:
                if os.unlink in os.supports_dir_fd:
                    os.unlink(tmp_name, dir_fd=parent_descriptor)
                else:  # pragma: no cover - exercised only without unlinkat
                    (parent / tmp_name).unlink()
            except OSError:
                pass
            raise

        try:
            os.fsync(parent_descriptor)
        except OSError:  # pragma: no cover - best-effort durability only
            pass

        # Post-write binding validation: confirm the name we just published
        # still resolves to a real, regular file (never a symlink/special
        # file) without following it. A concurrent writer may have already
        # published its own complete content over ours by the time this
        # check runs -- that is correct atomic-write interleaving, not
        # corruption, so this only re-checks *shape*, never our own bytes.
        verified = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(verified.st_mode):
            raise AtomicWriteIntegrityError(f"原子写入目标未通过写后绑定核验： {path}")
    finally:
        os.close(parent_descriptor)


def _atomic_write_fallback(path: Path, data: bytes | Iterable[bytes]) -> None:
    """Atomic write fallback with portable ancestor/reparse-point checks.

    Native Windows lacks the POSIX ``dir_fd`` publication primitive. It can
    still reject every static ancestor reparse point and compare directory
    identities before and after publication instead of blindly following a
    linked parent.
    """
    path = _absolute_target_path(path)
    parent_snapshot = _ensure_portable_atomic_parent(path.parent)
    _reject_unsafe_atomic_write_target(None, path.name, path)
    tmp = path.with_name(_atomic_write_temp_name(path.name))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in (data,) if isinstance(data, bytes) else data:
                handle.write(chunk)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - best-effort durability only
                pass
        _assert_portable_atomic_parent(path.parent, parent_snapshot)
        _reject_unsafe_atomic_write_target(None, path.name, path)
        os.replace(tmp, path)
        _assert_portable_atomic_parent(path.parent, parent_snapshot)
        _reject_unsafe_atomic_write_target(None, path.name, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Write ``text`` to ``path`` atomically (temp file + fsync + rename + fsync).

    A reader either sees the old complete file or the new complete file, never a
    half-written one -- so a crashed/concurrent write can't leave a torn cache.
    Hardened beyond plain temp+replace: the temp name carries a random nonce (not
    just the writer's PID, which can collide across a fork boundary or PID
    reuse), the temp file is fsynced before the rename and the parent directory
    is fsynced after it (durability across a crash), a symlink or special file
    already at the target is rejected rather than written through, and a parent-
    directory swap between temp-file creation and the final rename is detected
    and fails closed. None of this changes behavior for a well-formed,
    non-adversarial write to a regular file or absent path.
    """
    p = Path(path)
    absolute = _absolute_target_path(p)
    data = text.encode(encoding)
    if hasattr(os, "O_DIRECTORY") and hasattr(os, "fdopen"):
        _atomic_write_posix(absolute.parent, absolute.name, data, absolute)
    else:  # pragma: no cover - exercised only on platforms without O_DIRECTORY
        _atomic_write_fallback(absolute, data)
    return p


def atomic_write_chunks(path: str | Path, chunks: Iterable[str]) -> Path:
    """Stream a large UTF-8 export through the same bound atomic publication.

    Iterator failure leaves the previous complete export untouched. The input
    need not be copied into a second object or serialized into one giant string.
    """
    p = Path(path)
    absolute = _absolute_target_path(p)
    data = (chunk.encode("utf-8") for chunk in chunks)
    if hasattr(os, "O_DIRECTORY") and hasattr(os, "fdopen"):
        _atomic_write_posix(absolute.parent, absolute.name, data, absolute)
    else:
        _atomic_write_fallback(absolute, data)
    return p


def _atomic_create_posix(parent: Path, name: str, data: bytes, path: Path) -> None:
    """Publish one fully durable regular file without replacing an existing name."""
    parent_descriptor = _open_atomic_write_parent(parent)
    tmp_name = _atomic_write_temp_name(name)
    temp_exists = False
    try:
        _reject_unsafe_atomic_write_target(parent_descriptor, name, path)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(tmp_name, flags, 0o600, dir_fd=parent_descriptor)
        temp_exists = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        _assert_atomic_write_parent_binding(parent, parent_descriptor)
        # Hard-link publication is atomic and has no-clobber semantics on both
        # POSIX and Windows filesystems. The state commit may therefore refer to
        # either the complete immutable event or no event, never a torn event.
        if os.link in os.supports_dir_fd:
            os.link(
                tmp_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        else:  # pragma: no cover - only platforms without linkat
            os.link(parent / tmp_name, path)

        if os.unlink in os.supports_dir_fd:
            os.unlink(tmp_name, dir_fd=parent_descriptor)
        else:  # pragma: no cover - only platforms without unlinkat
            (parent / tmp_name).unlink()
        temp_exists = False
        try:
            os.fsync(parent_descriptor)
        except OSError:  # pragma: no cover - best-effort directory durability
            pass
        published = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode):
            raise AtomicWriteIntegrityError(f"原子创建目标未通过绑定核验： {path}")
    finally:
        if temp_exists:
            try:
                if os.unlink in os.supports_dir_fd:
                    os.unlink(tmp_name, dir_fd=parent_descriptor)
                else:  # pragma: no cover - only platforms without unlinkat
                    (parent / tmp_name).unlink()
            except OSError:
                pass
        os.close(parent_descriptor)


def _atomic_create_fallback(path: Path, data: bytes) -> None:
    """No-clobber publication with portable ancestor/reparse-point checks."""
    path = _absolute_target_path(path)
    parent_snapshot = _ensure_portable_atomic_parent(path.parent)
    _reject_unsafe_atomic_write_target(None, path.name, path)
    tmp = path.with_name(_atomic_write_temp_name(path.name))
    descriptor = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - best-effort durability
                pass
        _assert_portable_atomic_parent(path.parent, parent_snapshot)
        _reject_unsafe_atomic_write_target(None, path.name, path)
        os.link(tmp, path)
        _assert_portable_atomic_parent(path.parent, parent_snapshot)
        _reject_unsafe_atomic_write_target(None, path.name, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def atomic_create_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Atomically create an immutable text file and fail if it already exists."""
    target = Path(path)
    absolute = _absolute_target_path(target)
    data = text.encode(encoding)
    if hasattr(os, "O_DIRECTORY") and hasattr(os, "fdopen"):
        _atomic_create_posix(
            absolute.parent,
            absolute.name,
            data,
            absolute,
        )
    else:  # pragma: no cover - exercised on native Windows CI
        _atomic_create_fallback(absolute, data)
    return target


__all__ = [
    "AtomicWriteIntegrityError",
    "atomic_create_text",
    "atomic_write_chunks",
    "atomic_write_text",
]
