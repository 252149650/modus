"""Sanitize source-derived text before it crosses the N5 publication boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence, Set
from typing import Any

from modus.sensitive_literals import redact_sensitive_text

# These fields are structural coordinates or content-derived identities. Rewriting
# one would sever the evidence link; if one ever contains prohibited text, N6 must
# reject it and report the safe JSON pointer instead.
_IDENTITY_FIELDS = frozenset(
    {
        "analysisContextId",
        "baselineDigest",
        "candidateId",
        "candidateIds",
        "capabilityId",
        "capabilityIds",
        "contextId",
        "entryId",
        "file",
        "graphNodeId",
        "id",
        "path",
        "proposalId",
        "recordId",
        "ruleId",
        "sourceDigest",
        "sourceFile",
        "source_file",
    }
)


# 返回独立的 JSON 形状副本，脱敏不能修改上游冻结证据。
# 统计只记录敏感类别，原始文本不得进入报告中的诊断计数。
def sanitize_publication(value: Any, *, key: str = "") -> tuple[Any, Counter[str]]:
    """Return a JSON-shaped copy with prohibited source spans safely redacted."""

    counts: Counter[str] = Counter()

    def sanitize(current: Any, current_key: str = "") -> Any:
        if isinstance(current, str):
            if current_key in _IDENTITY_FIELDS:
                return current
            redacted, kinds = redact_sensitive_text(current)
            counts.update(kinds)
            return redacted
        if isinstance(current, Mapping):
            return {str(name): sanitize(item, str(name)) for name, item in current.items()}
        if isinstance(current, (Sequence, Set)) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            return [sanitize(item, current_key) for item in current]
        return current

    return sanitize(value, key), counts


__all__ = ["sanitize_publication"]
