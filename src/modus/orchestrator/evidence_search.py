"""Bounded source and knowledge search with interchangeable backends.

This module owns search execution. Query understanding lives in
``query_terms`` and filesystem/data-protection rules live in ``search_policy``.
Keeping those policies outside the backend makes it difficult for a new search
engine to accidentally bypass scope checks or secret redaction.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from ..knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from ..paths import MANAGED_ROOT_FILES, ModusPaths
from .models import FallbackResult, SearchHit, SearchIssue
from .query_terms import extract_terms, target_search_tokens
from .search_policy import (
    EXCLUDED_SOURCE_DIRECTORIES,
    iter_markdown_files,
    iter_source_files,
    path_is_allowed,
    redact_secrets,
    search_roots,
    source_suffixes,
)

_SOURCE_READ_LIMIT_BYTES = 2 * 1024 * 1024
_KNOWLEDGE_READ_LIMIT_BYTES = 32 * 1024 * 1024
_DEFAULT_RG_TIMEOUT_SECONDS = 30.0
# 限制单文件证据占比，避免一个重复词文件填满全部结果。
_MAX_RG_MATCHES_PER_FILE = 3
_MAX_EVIDENCE_LINE_CHARS = 200


# 可注入执行器让命令构造与进程运行分离，便于验证预算和参数边界。
class ProcessRunner(Protocol):
    """The narrow subprocess surface used by the ripgrep backend."""

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Execute the argument vector with the caller's search budget."""


ExecutableLocator = Callable[[str], str | None]


# 显式文档集合就是检索范围，缺失目标不触发扩大扫描。
# 快速后端失败后保留诊断，并在原截止时间内尝试 Python 回退。
def search_knowledge(
    root: str | Path,
    requirement: str,
    *,
    doc_targets: Sequence[Path],
    locate_executable: ExecutableLocator,
    run_process: ProcessRunner,
    max_results: int = 10,
    prefer_rg: bool = True,
    timeout_seconds: float | None = None,
    terms: Sequence[str] | None = None,
    deadline_monotonic: float | None = None,
) -> FallbackResult:
    """Search caller-selected canonical Markdown knowledge.

    Knowledge targets are explicit. Missing or invalid targets produce no
    evidence instead of broadening the scan to other documents.
    """
    paths = ModusPaths.for_root(root)
    base_terms = list(terms) if terms is not None else extract_terms(requirement, limit=64)
    search_terms, rewritten = _with_rewritten_terms(base_terms, ())
    deadline = _deadline(timeout_seconds, deadline_monotonic)

    stopped = _stopped_result(search_terms, max_results, deadline, channel="knowledge")
    if stopped is not None:
        return stopped

    issues: list[SearchIssue] = []
    if prefer_rg and locate_executable("rg") is not None:
        try:
            hits = _ripgrep_knowledge(
                paths.root,
                doc_targets,
                search_terms,
                max_results=max_results,
                timeout_seconds=_remaining_timeout(deadline),
                run_process=run_process,
            )
            return FallbackResult(
                terms=search_terms,
                hits=hits,
                engine="rg",
                base_terms=base_terms,
                rewritten_terms=rewritten,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            issues.append(_search_issue(exc, channel="knowledge"))

    hits = _python_knowledge_search(
        paths.root,
        doc_targets,
        search_terms,
        max_results=max_results,
        deadline_monotonic=deadline,
    )
    return FallbackResult(
        terms=search_terms,
        hits=hits,
        engine="python",
        base_terms=base_terms,
        rewritten_terms=rewritten,
        timed_out=_expired(deadline),
        issues=issues,
    )


# 检索词扩展与路径范围分别处理，补充词不会扩大允许读取的目录。
# 两个后端共享同一截止时间，回退不重新获得完整预算。
def search_source(
    root: str | Path,
    requirement: str,
    *,
    locate_executable: ExecutableLocator,
    run_process: ProcessRunner,
    max_results: int = 20,
    prefer_rg: bool = True,
    timeout_seconds: float | None = None,
    extra_terms: Sequence[str] = (),
    path_prefixes: Sequence[str] = (),
    deadline_monotonic: float | None = None,
) -> FallbackResult:
    """Search current source using bounded request and routing vocabulary."""
    paths = ModusPaths.for_root(root)
    base_terms = extract_terms(requirement, limit=16)
    search_terms, rewritten = _with_rewritten_terms(base_terms, extra_terms)
    deadline = _deadline(timeout_seconds, deadline_monotonic)

    stopped = _stopped_result(search_terms, max_results, deadline, channel="source")
    if stopped is not None:
        return stopped

    prefixes = [str(item) for item in path_prefixes if item]
    issues: list[SearchIssue] = []
    if prefer_rg and locate_executable("rg") is not None:
        try:
            hits = _ripgrep_source(
                paths.root,
                search_terms,
                max_results=max_results,
                timeout_seconds=_remaining_timeout(deadline),
                path_prefixes=prefixes,
                run_process=run_process,
            )
            return FallbackResult(
                terms=search_terms,
                hits=_prefer_application_source(hits),
                engine="rg",
                base_terms=base_terms,
                rewritten_terms=rewritten,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            issues.append(_search_issue(exc, channel="source"))

    hits = _python_source_search(
        paths.root,
        search_terms,
        max_results=max_results,
        deadline_monotonic=deadline,
        path_prefixes=prefixes,
    )
    return FallbackResult(
        terms=search_terms,
        hits=_prefer_application_source(hits),
        engine="python",
        base_terms=base_terms,
        rewritten_terms=rewritten,
        timed_out=_expired(deadline),
        issues=issues,
    )


# 逐文件检查预算，并通过受约束读取限制单文件大小与路径边界。
# 结果达到上限即返回，避免为展示少量证据遍历完整仓库。
def _python_source_search(
    root: Path,
    terms: Sequence[str],
    *,
    max_results: int,
    deadline_monotonic: float | None,
    path_prefixes: Sequence[str],
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    lowered_terms = [(term, term.lower()) for term in terms]
    for path in iter_source_files(root):
        if _expired(deadline_monotonic):
            return hits
        relative = path.relative_to(root).as_posix()
        if path_prefixes and not path_is_allowed(relative, path_prefixes):
            continue
        try:
            text = read_bound_regular_text(root, path, max_bytes=_SOURCE_READ_LIMIT_BYTES)
        except (BoundKnowledgeReadError, OSError, ValueError):
            continue
        hits.extend(
            _line_hits(
                text,
                relative=relative,
                terms=lowered_terms,
                remaining=max_results - len(hits),
                deadline_monotonic=deadline_monotonic,
            )
        )
        if len(hits) >= max_results:
            return hits
    return hits


# 只遍历调用方提供的 Markdown 目标，读取失败不改变目标集合。
def _python_knowledge_search(
    root: Path,
    targets: Sequence[Path],
    terms: Sequence[str],
    *,
    max_results: int,
    deadline_monotonic: float | None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    lowered_terms = [(term, term.lower()) for term in terms]
    for path in iter_markdown_files(targets):
        if _expired(deadline_monotonic):
            return hits
        try:
            text = read_bound_regular_text(root, path, max_bytes=_KNOWLEDGE_READ_LIMIT_BYTES)
        except (BoundKnowledgeReadError, OSError, ValueError):
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = str(path)
        hits.extend(
            _line_hits(
                text,
                relative=relative,
                terms=lowered_terms,
                remaining=max_results - len(hits),
                deadline_monotonic=deadline_monotonic,
            )
        )
        if len(hits) >= max_results:
            return hits
    return hits


# 逐行返回局部证据并脱敏，输出不会复制完整源码文件。
# 每隔一批行检查截止时间，在检查成本与响应速度间保持平衡。
def _line_hits(
    text: str,
    *,
    relative: str,
    terms: Sequence[tuple[str, str]],
    remaining: int,
    deadline_monotonic: float | None,
) -> list[SearchHit]:
    """Find bounded line evidence with periodic cooperative deadline checks."""
    hits: list[SearchHit] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line_number % 256 == 0 and _expired(deadline_monotonic):
            return hits
        lowered_line = line.lower()
        matched = next((term for term, lowered in terms if lowered in lowered_line), None)
        if matched is None:
            continue
        hits.append(
            SearchHit(
                file=relative,
                line=line_number,
                text=redact_secrets(line.strip()[:_MAX_EVIDENCE_LINE_CHARS]),
                term=matched,
            )
        )
        if len(hits) >= remaining:
            return hits
    return hits


# 命令采用参数数组；检索词转义为字面匹配，路径置于选项终止符之后。
# 文件类型、目录排除和大小限制在启动进程前统一配置。
def _ripgrep_source(
    root: Path,
    terms: Sequence[str],
    *,
    max_results: int,
    timeout_seconds: float,
    path_prefixes: Sequence[str],
    run_process: ProcessRunner,
) -> list[SearchHit]:
    globs: list[str] = []
    for suffix in sorted(source_suffixes()):
        globs.extend(("-g", f"*{suffix}"))
    for directory in sorted(EXCLUDED_SOURCE_DIRECTORIES):
        globs.extend(("-g", f"!**/{directory}/**"))
    for name in sorted(MANAGED_ROOT_FILES):
        globs.extend(("-g", f"!/{name}"))
    globs.extend(("-g", "!/modus/**", "-g", "!/.modus/**"))

    # 实际进程根目录来自统一范围策略，而非直接使用请求中的路径。
    roots = search_roots(root, path_prefixes)
    if not roots:
        return []
    command = [
        "rg",
        "--no-heading",
        "--max-filesize",
        "2M",
        "--max-columns",
        "2000",
        "--with-filename",
        "--line-number",
        "-i",
        "-e",
        _pattern(terms),
        "--max-count",
        str(_MAX_RG_MATCHES_PER_FILE),
        *globs,
        "--",
        *[str(item.relative_to(root)) for item in roots],
    ]
    process = run_process(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=max(0.01, timeout_seconds),
    )
    return _parse_ripgrep(process, command, root, terms, max_results=max_results)


# 知识文档单独设置大小上限，并强制输出文件名以兼容单文件目标。
# 显式目标位于 -- 之后，避免路径被解释为命令选项。
def _ripgrep_knowledge(
    root: Path,
    targets: Sequence[Path],
    terms: Sequence[str],
    *,
    max_results: int,
    timeout_seconds: float,
    run_process: ProcessRunner,
) -> list[SearchHit]:
    existing = [str(target) for target in targets if target.exists()]
    if not existing:
        return []
    command = [
        "rg",
        "--no-heading",
        "--max-filesize",
        "32M",
        "--max-columns",
        "2000",
        # Required because rg omits a filename for one explicit file target.
        "--with-filename",
        "--line-number",
        "-i",
        "-e",
        _pattern(terms),
        "-g",
        "*.md",
        "--max-count",
        str(_MAX_RG_MATCHES_PER_FILE),
        "--",
        *existing,
    ]
    process = run_process(
        command,
        capture_output=True,
        text=True,
        timeout=max(0.01, timeout_seconds),
    )
    return _parse_ripgrep(process, command, root, terms, max_results=max_results)


# 退出码 1 代表没有命中，其他失败交给上层执行回退。
# 后端输出统一转换成相同证据结构并脱敏。
def _parse_ripgrep(
    process: subprocess.CompletedProcess[str],
    command: list[str],
    root: Path,
    terms: Sequence[str],
    *,
    max_results: int,
) -> list[SearchHit]:
    if process.returncode not in (0, 1):
        raise subprocess.CalledProcessError(process.returncode, command, stderr=process.stderr)

    hits: list[SearchHit] = []
    for raw in process.stdout.splitlines():
        # 只切分前两个分隔符，保留命中文本中的冒号。
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        file_name, line_number, text = parts
        try:
            relative = (root / file_name).resolve().relative_to(root).as_posix()
        except ValueError:
            relative = file_name
        # 后端仅返回文本，命中词由同一词表重新关联以统一结果协议。
        matched = next((term for term in terms if term.lower() in text.lower()), terms[0])
        hits.append(
            SearchHit(
                file=relative,
                line=int(line_number) if line_number.isdigit() else 0,
                text=redact_secrets(text.strip()[:_MAX_EVIDENCE_LINE_CHARS]),
                term=matched,
            )
        )
        if len(hits) >= max_results:
            break
    return hits


# 扩展词按大小写无关形式去重，同时单独保留扩展来源供诊断。
def _with_rewritten_terms(
    base_terms: Sequence[str],
    targets: Sequence[str],
) -> tuple[list[str], list[str]]:
    # 保留原始词形用于展示，规范形式仅用于去重。
    known = {term.lower() for term in base_terms}
    rewritten: list[str] = []
    for target in targets:
        candidates = target_search_tokens(str(target)) or [str(target)]
        for candidate in candidates:
            if candidate.lower() not in known:
                known.add(candidate.lower())
                rewritten.append(candidate)
    return [*base_terms, *rewritten], rewritten


# 空词不启动搜索，零预算或过期请求返回可解释的停止状态。
def _stopped_result(
    terms: list[str],
    max_results: int,
    deadline: float | None,
    *,
    channel: str,
) -> FallbackResult | None:
    if not terms:
        return FallbackResult(terms=[])
    if max_results > 0 and not _expired(deadline):
        return None
    issue = SearchIssue(type=f"{channel}-budget", reason="search-deadline")
    return FallbackResult(terms=terms, timed_out=True, issues=[issue])


# 局部预算与共享请求预算取较早者，子阶段不能延长父请求生命周期。
def _deadline(timeout_seconds: float | None, shared_deadline: float | None) -> float | None:
    local = time.monotonic() + max(0.0, timeout_seconds) if timeout_seconds is not None else None
    candidates = [value for value in (local, shared_deadline) if value is not None]
    return min(candidates) if candidates else None


# 将绝对截止时间转换为进程超时，保证子进程获得有限运行时间。
def _remaining_timeout(deadline: float | None) -> float:
    # 即使调用方未设置截止时间，外部进程仍有默认超时。
    if deadline is None:
        return _DEFAULT_RG_TIMEOUT_SECONDS
    return max(0.01, deadline - time.monotonic())


# 使用单调时钟，系统时间调整不会改变搜索预算。
def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


# 只交付结构化失败类别和退出码，避免把进程输出带入用户证据。
# 回退信息与命中证据并列返回，调用方能识别搜索能力的降级。
def _search_issue(exc: Exception, *, channel: str) -> SearchIssue:
    issue = SearchIssue(
        type=f"{channel}-search",
        engine="rg",
        reason="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "execution-failed",
        fallback="python",
    )
    if isinstance(exc, subprocess.CalledProcessError):
        issue["exitCode"] = exc.returncode
    return issue


# 对每个词进行正则转义，用户文本不会改变匹配表达式的语义。
# 词之间只使用或关系，单个词中的元字符不具有正则控制权。
def _pattern(terms: Sequence[str]) -> str:
    return "|".join(re.escape(term) for term in terms)


# 只调整已授权命中的展示顺序，不增加新的搜索目标。
def _prefer_application_source(hits: list[SearchHit]) -> list[SearchHit]:
    """Keep application source ahead of SQL/documentation when results mix."""

    def rank(hit: SearchHit) -> tuple[int, str]:
        path = hit["file"].replace("\\", "/").lower()
        is_sql = int(path.endswith(".sql") or "/docs/sql/" in path or "/sql/" in path)
        return is_sql, path

    # 稳定排序保留同一文件内部命中次序，减少相邻证据的跳动。
    return sorted(hits, key=rank)
