"""Typed boundary models shared by orchestration services.

The orchestrator ultimately emits JSON-compatible dictionaries because its
consumers are CLI commands and agent protocols.  Internal collaboration uses
named models wherever a stable concept exists; ``JsonObject`` is reserved for
the intentionally open protocol boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, TypedDict

JsonObject: TypeAlias = dict[str, Any]
SearchEngine: TypeAlias = Literal["rg", "python", "none"]


class SearchHit(TypedDict):
    """One bounded, redacted line of project evidence."""

    file: str
    line: int
    text: str
    term: str


class SearchIssue(TypedDict, total=False):
    """A machine-readable reason why a search backend degraded."""

    type: str
    engine: str
    reason: str
    fallback: str
    exitCode: int


@dataclass
class FallbackResult:
    """Evidence found by the dependency-free search fallback.

    ``FallbackResult`` is the historical public name and remains stable.  The
    class is deliberately small: policy belongs to the search service, while
    this value object only records what happened.
    """

    terms: list[str]
    hits: list[SearchHit] = field(default_factory=list)
    engine: SearchEngine = "none"
    base_terms: list[str] = field(default_factory=list)
    rewritten_terms: list[str] = field(default_factory=list)
    timed_out: bool = False
    issues: list[SearchIssue] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        """Return hit files in first-evidence order without duplicates."""
        return list(dict.fromkeys(hit["file"] for hit in self.hits))

    def gap_note(self) -> str:
        """Explain the evidence gap in language suitable for an agent report."""
        if not self.terms:
            return "ripgrep 回退：未从需求提取到可搜索词"
        if not self.hits:
            if self.rewritten_terms:
                return f"ripgrep 回退未找到以下查询的源码匹配：{self.terms}（包含源码锚点改写：{self.rewritten_terms}）；词汇映射无法为该需求找到证据，请记录词汇缺口"
            return f"ripgrep 回退未找到以下查询的源码匹配：{self.terms}；请继续定向调查知识主定义与当前源码"
        return f"使用 ripgrep 回退搜索：{self.terms} ({len(self.files)} 个文件）；请将相关发现归入已确认领域"


# Prefer this semantic name in new code while retaining the established public
# protocol name above for integrations.
EvidenceSearchResult = FallbackResult
