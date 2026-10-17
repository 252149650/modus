"""Snapshot-bound source spans shared by reconciliation and evidence projections."""

from __future__ import annotations

import hashlib
import re
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Any


class SourceCoordinates:
    """Two-file LRU for byte-coordinate conversion, never name-based binding."""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root is not None else None
        self.files: OrderedDict[str, tuple[bytes, array[int], str]] = OrderedDict()

    def span(self, location: dict[str, Any]) -> tuple[str, str, int, int] | None:
        """Resolve an observed location to a nonempty span in captured UTF-8 bytes.

        Successful conversion enriches the supplied location with offsets and its
        source digest. Unknown encodings, invalid codepoint boundaries and unavailable
        input return None so callers cannot claim a guessed correspondence.
        """

        relative = location["path"]
        if self.root is None:
            return None
        try:
            from modus.machine.corpus.input_snapshot import read_bytes

            if relative not in self.files:
                raw = read_bytes(self.root, self.root / relative)
                offsets = array("I", [0])
                offsets.extend(match.end() for match in re.finditer(b"\n", raw))
                self.files[relative] = (raw, offsets, hashlib.sha256(raw).hexdigest())
                if len(self.files) > 2:
                    self.files.popitem(last=False)
            self.files.move_to_end(relative)
            raw, offsets, digest = self.files[relative]
            start, end = location.get("startByte"), location.get("endByte")
            if type(start) is not int or type(end) is not int:
                converted = _line_column_span(raw, offsets, location)
                if converted is None:
                    return None
                start, end = converted
            if not (0 <= start < end <= len(raw)):
                return None
            location.update(startByte=start, endByte=end, sourceDigest=digest)
            return (relative, digest, start, end)
        except (OSError, ValueError, TypeError, UnicodeError):
            return None


def _line_column_span(
    raw: bytes, offsets: array[int], location: dict[str, Any]
) -> tuple[int, int] | None:
    """列编码转换只产生坐标；缓存更新和位置发布仍由调用方负责。"""
    values: list[int] = []
    encoding = location.get("columnEncoding", "unknown")
    for line, column in (
        (location.get("startLine"), location.get("startColumn")),
        (location.get("endLine"), location.get("endColumn")),
    ):
        if (
            type(line) is not int
            or type(column) is not int
            or line < 1
            or column < 1
            or line > len(offsets)
        ):
            return None
        offset = offsets[line - 1]
        stop = offsets[line] if line < len(offsets) else len(raw)
        text = raw[offset:stop]
        if encoding in {"utf8-byte", "utf-8-byte-column"} or text.isascii():
            prefix = text[: column - 1]
            if len(prefix) != column - 1:
                return None
            # A column into a multi-byte codepoint is not a valid anchor.
            prefix.decode("utf-8")
        elif encoding in {"utf16", "utf-16"}:
            utf16 = text.decode("utf-8").encode("utf-16-le")
            prefix = utf16[: 2 * (column - 1)].decode("utf-16-le").encode("utf-8")
            if 2 * (column - 1) > len(utf16):
                return None
        elif encoding in {"unicode-codepoint", "codepoint"}:
            unicode_text = text.decode("utf-8")
            if column - 1 > len(unicode_text):
                return None
            prefix = unicode_text[: column - 1].encode("utf-8")
        else:
            return None
        values.append(offset + len(prefix))
    return values[0], values[1]
