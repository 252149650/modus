"""Language-aware extraction of high-signal retrieval terms."""

from __future__ import annotations

import re
from collections import Counter

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_EXACT_SIGNAL_RE = re.compile(
    r"/[A-Za-z0-9_.~!$&'()*+,;=:@%/-]{2,}|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:[.:/#-][A-Za-z0-9_]+)+|"
    r"\b(?:[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b"
)

# Generic request language is routing noise.  Domain terms are intentionally
# not guessed here: a false global stop word can erase a real product concept.
_STOP_WORDS = frozenset(
    {
        "add",
        "the",
        "and",
        "for",
        "with",
        "support",
        "feature",
        "implement",
        "create",
        "update",
        "new",
        "into",
        "from",
        "this",
        "that",
        "should",
        "able",
        "when",
        "then",
        "需求",
        "功能",
        "支持",
        "实现",
        "增加",
        "tapd",
        "story",
        "stories",
    }
)
_CJK_PREFIX_STOP_WORDS = ("支持", "实现", "增加", "新增", "创建", "更新")
_CJK_NOISE = frozenset(
    {"需求", "页面", "展示", "提示", "进行", "相关", "本次", "这里", "可以", "应该"}
)


def has_explicit_locator(requirement: str) -> bool:
    """Return whether a request names a path or code-like symbol.

    HTTP URLs are removed first because task-tracker links are metadata, not
    source locators.
    """
    scrubbed = re.sub(r"https?://\S+", " ", requirement, flags=re.IGNORECASE)
    return _EXACT_SIGNAL_RE.search(scrubbed) is not None


def extract_terms(requirement: str, *, limit: int = 16) -> list[str]:
    """Rank signals from the complete request, then apply the result budget.

    Long issue descriptions often put discriminating vocabulary after process
    metadata.  Exhaustive collection followed by scoring prevents the opening
    prose from consuming the caller's entire term budget.
    """
    if limit <= 0:
        return []

    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    signal_strength: dict[str, float] = {}

    def add(term: str, *, base: float) -> None:
        normalized = term.casefold().strip()
        if not normalized or normalized in _STOP_WORDS or normalized in _CJK_NOISE:
            return
        first_seen.setdefault(normalized, len(first_seen))
        counts[normalized] += 1
        signal_strength[normalized] = max(signal_strength.get(normalized, 0.0), base)

    for signal in _EXACT_SIGNAL_RE.findall(requirement):
        add(signal, base=40.0 + min(len(signal), 24))
    for token in _TOKEN_RE.findall(requirement):
        add(token, base=22.0 + min(len(token), 16))
    for raw in _CJK_RE.findall(requirement):
        term = _strip_request_prefix(raw)
        if 2 <= len(term) <= 12:
            add(term, base=8.0 + min(len(term), 8))
        for width in range(2, min(4, len(term)) + 1):
            for offset in range(len(term) - width + 1):
                phrase = term[offset : offset + width]
                if phrase != term:
                    add(phrase, base=4.0 + width * 1.5)

    ranked = sorted(
        counts,
        key=lambda value: (
            -(signal_strength[value] + min(max(counts[value] - 1, 0), 7) * 12.0),
            -len(value),
            first_seen[value],
            value,
        ),
    )
    return ranked[:limit]


def target_search_tokens(target: str) -> list[str]:
    """Extract searchable identifiers from a source anchor or path."""
    tail = target.rsplit("/", 1)[-1]
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    return _TOKEN_RE.findall(stem)


def _strip_request_prefix(term: str) -> str:
    for prefix in _CJK_PREFIX_STOP_WORDS:
        if term.startswith(prefix) and len(term) > len(prefix):
            return term[len(prefix) :]
    return term
