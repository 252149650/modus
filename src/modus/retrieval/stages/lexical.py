"""Lexical tokenization, document statistics and BM25 scoring."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, TypedDict

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{1,}|[\u4e00-\u9fff]{2,}")
_BM25_K1 = 1.2
_BM25_K1_PLUS_ONE = 2.2
_BM25_LENGTH_NORMALIZATION = 0.75
_BM25_BASE_LENGTH = 0.25


class LexicalIndex(TypedDict):
    documentCount: int
    averageLength: float
    documentFrequency: dict[str, int]
    documentLengths: dict[str, int]
    terms: dict[str, dict[str, int]]


# 中文长词同时保留原词与相邻双字片段，支持没有空格的短语检索。
def tokenize(text: str) -> list[str]:
    """Tokenize identifiers and CJK prose without language-specific services."""

    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.casefold()):
        token = match.group(0)
        tokens.append(token)
        if any("\u4e00" <= char <= "\u9fff" for char in token) and len(token) > 2:
            tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
    return tokens


# 文档频率按出现过的词计数，不能把单篇内重复次数当成更多文档。
def build_lexical_index(records: list[dict[str, Any]]) -> LexicalIndex:
    """Build deterministic document statistics used by query-local BM25."""

    document_frequency: Counter[str] = Counter()
    lengths: dict[str, int] = {}
    terms: dict[str, dict[str, int]] = {}
    for record in records:
        counts = Counter(
            tokenize(
                " ".join(
                    [
                        record["path"],
                        record["heading"],
                        " ".join(record.get("headingPath") or []),
                        str(record.get("frontmatter") or ""),
                        record["text"],
                    ]
                )
            )
        )
        terms[record["id"]] = dict(sorted(counts.items()))
        lengths[record["id"]] = sum(counts.values())
        document_frequency.update(counts.keys())
    return {
        "documentCount": len(records),
        "averageLength": (sum(lengths.values()) / len(lengths)) if lengths else 0.0,
        "documentFrequency": dict(sorted(document_frequency.items())),
        "documentLengths": lengths,
        "terms": terms,
    }


@dataclass(slots=True)
class BM25Query:
    """Query-local IDF memo; token order and floating-point operations stay fixed.

    Compute IDF only when an eligible record actually contains the token. Empty
    results and malformed, unused statistics keep their previous failure behavior.
    """

    index: dict[str, Any]
    tokens: list[str]
    _inverse: dict[tuple[int, str], float] = field(default_factory=dict, init=False)

    # 无词频时跳过 IDF 计算，空文档与无关统计不会触发多余数值转换。
    def score(self, record: dict[str, Any]) -> float:
        lexical = self.index["lexical"]
        counts = lexical.get("terms", {}).get(record["id"], {})
        length = float(lexical.get("documentLengths", {}).get(record["id"], 0) or 0)
        average = float(lexical.get("averageLength", 0.0) or 1.0)
        total = max(1, int(lexical.get("documentCount", 0) or 0))
        score = 0.0
        for token in self.tokens:
            frequency = float(counts.get(token, 0) or 0)
            if not frequency:
                continue
            key = (total, token)
            if key not in self._inverse:
                documents = float(lexical.get("documentFrequency", {}).get(token, 0) or 0)
                self._inverse[key] = math.log(1.0 + (total - documents + 0.5) / (documents + 0.5))
            inverse = self._inverse[key]
            score += (
                inverse
                * (frequency * _BM25_K1_PLUS_ONE)
                / (
                    frequency
                    + _BM25_K1 * (_BM25_BASE_LENGTH + _BM25_LENGTH_NORMALIZATION * length / average)
                )
            )
        return score


def _bm25(index: dict[str, Any], query_tokens: list[str], record: dict[str, Any]) -> float:
    return BM25Query(index, query_tokens).score(record)


# Compatibility with the original internal builder name.
_lexical = build_lexical_index

__all__ = ["BM25Query", "LexicalIndex", "build_lexical_index", "tokenize"]
