"""Minimal semantic-version handling for framework + manifest versioning.

Kept dependency-free on purpose: explicit updates must be able to compare the
installed framework version against a user project's recorded version without
pulling in heavy parsing libraries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

_VERSION_RE: Final = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
VersionChange = Literal["major", "minor", "patch", "none", "downgrade"]


@dataclass(frozen=True, order=True, slots=True)
class Version:
    """Comparable semantic-version core, excluding prerelease/build metadata."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = _VERSION_RE.match(value.strip())
        if not match:
            raise ValueError(f"语义版本无效： {value!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def bump_kind(old: str, new: str) -> VersionChange:
    """Classify the change from ``old`` to ``new``.

    Returns one of ``"major"``, ``"minor"``, ``"patch"``, ``"none"`` or
    ``"downgrade"``. Silent updates may apply patch/minor automatically but must
    refuse major changes unless explicitly accepted. While Modus is still in the
    ``0.x`` series, minor bumps are treated as major for update-safety because
    they may carry breaking changes.
    """
    o = Version.parse(old)
    n = Version.parse(new)
    if n < o:
        return "downgrade"
    if n == o:
        return "none"
    if n.major != o.major:
        return "major"
    if n.minor != o.minor:
        if o.major == 0:
            return "major"
        return "minor"
    return "patch"


__all__ = ["Version", "VersionChange", "bump_kind"]
