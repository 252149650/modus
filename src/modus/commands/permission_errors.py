"""Shared classification helpers for update permission failures."""

from __future__ import annotations

import errno

__all__ = ["is_permission_error", "output_indicates_permission_error"]

PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
PERMISSION_OUTPUT_MARKERS = (
    "access is denied",
    "operation not permitted",
    "permission denied",
    "read-only file system",
)


def is_permission_error(exc: BaseException) -> bool:
    """Return whether an exception represents an OS-level permission denial."""
    return isinstance(exc, OSError) and exc.errno in PERMISSION_ERRNOS


def output_indicates_permission_error(text: str) -> bool:
    """Recognize package-manager permission failures from bounded process output."""
    normalized = text.casefold()
    return any(marker in normalized for marker in PERMISSION_OUTPUT_MARKERS)
