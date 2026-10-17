"""Layer-scoped implementation identities for precise cache invalidation.

An implementation digest answers "which code can change this product?".  The
six lifecycle stages are hashed independently so changing N6 packet assembly
does not invalidate N2 compiler facts.  A composite digest still authenticates
the published bundle as a whole.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Final

from .errors import InvariantError

_LAYERS: Final = ("corpus", "facts", "resolution", "capability", "projection", "packet")
_CROSS_CUTTING: Final = ("store", "runtime")
_SOURCE_SUFFIXES: Final = frozenset({".py", ".json", ".java", ".go", ".cjs"})
_SHARED_FILES: Final = (
    "errors.py",
    "implementation.py",
    "language-registry.json",
    "languages.py",
    "receipts.py",
    "states.py",
)


def _digest_paths(base: Path, paths: tuple[Path, ...], *, namespace: str) -> str:
    state = hashlib.sha256(("modus-machine:" + namespace + "\n").encode())
    for path in paths:
        state.update(path.relative_to(base).as_posix().encode())
        state.update(b"\0")
        state.update(path.read_bytes())
        state.update(b"\n")
    return state.hexdigest()


def _implementation_files(base: Path, directory: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix in _SOURCE_SUFFIXES and "__pycache__" not in path.parts
    )


@cache
def layer_implementation_digest(layer: str) -> str:
    """Return the digest for one lifecycle or cross-cutting package."""
    if layer not in {*_LAYERS, *_CROSS_CUTTING, "shared", "pipeline"}:
        raise InvariantError(
            "machine.pipeline.unknown-implementation-layer",
            details={"layer": layer},
        )
    base = Path(__file__).parent
    if layer == "shared":
        paths = tuple(base / name for name in _SHARED_FILES)
    elif layer == "pipeline":
        paths = (base / "pipeline.py", base / "__init__.py")
    else:
        paths = _implementation_files(base, base / layer)
    return _digest_paths(base, paths, namespace=layer)


def implementation_digests() -> dict[str, str]:
    """Return every independently invalidatable implementation identity."""
    names = (*_LAYERS, *_CROSS_CUTTING, "shared", "pipeline")
    return {name: layer_implementation_digest(name) for name in names}


def implementation_digest() -> str:
    """Return the deterministic composite identity of the whole machine layer."""
    encoded = json.dumps(implementation_digests(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"modus-machine-composite\n" + encoded).hexdigest()


__all__ = [
    "implementation_digest",
    "implementation_digests",
    "layer_implementation_digest",
]
