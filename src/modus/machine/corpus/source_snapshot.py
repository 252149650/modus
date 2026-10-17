"""One safely read file, with syntax reuse confined to its extraction lifetime."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ContractError

if TYPE_CHECKING:
    from tree_sitter import Parser, Tree


@dataclass(eq=False, repr=False)
class SourceSnapshot:
    """Read source without reopening a file or retaining a project of ASTs.

    Path operations expose only metadata needed by adapters. The private syntax
    memo dies with this per-file snapshot; it never participates in persistent
    cache validity or final source verification.
    """

    path: Path
    content: str
    _trees: dict[tuple[object, bytes], Tree] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __str__(self) -> str:
        return str(self.path)

    def __fspath__(self) -> str:
        return str(self.path)

    def __getattr__(self, key: str) -> Any:
        # Preserve the Path-compatible facade used by existing adapters. New
        # extraction interfaces declare their inputs explicitly as SourcePath.
        return getattr(self.path, key)

    def read_bytes(self) -> bytes:
        return self.content.encode("utf-8")

    def read_text(self, encoding: str | None = None, errors: str | None = None) -> str:
        return self.content

    def open(self, mode: str = "r", *args: object, **kwargs: object) -> io.BytesIO | io.StringIO:
        if any(char in mode for char in "wa+"):
            raise ContractError("machine.corpus.snapshot-read-only")
        return io.BytesIO(self.read_bytes()) if "b" in mode else io.StringIO(self.content)

    def parse(self, parser: Parser, source: bytes) -> Tree:
        key = (parser.language, source)
        if key not in self._trees:
            self._trees[key] = parser.parse(source)
        return self._trees[key]


SourcePath = Path | SourceSnapshot


def parse_source(path: SourcePath, parser: Parser, source: bytes) -> Tree:
    """Reuse only the same immutable snapshot, grammar and exact input bytes."""
    if isinstance(path, SourceSnapshot):
        return path.parse(parser, source)
    return parser.parse(source)
