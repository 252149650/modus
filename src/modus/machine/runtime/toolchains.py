"""Local toolchain discovery: where compilers and SDK launchers actually are.

Locating executables is an environment concern shared by N1 (recording the
toolchains a round was analysed with) and N2 (running compiler providers).
It shapes cost and coverage, never output facts.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def java_executable(name: str) -> str | None:
    """Resolve Java tools from JAVA_HOME before generic system launchers."""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


__all__ = ["java_executable"]
