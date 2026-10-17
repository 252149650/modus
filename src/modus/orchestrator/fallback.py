"""Stable fallback-search facade for orchestration consumers.

Historically this module contained term extraction, traversal policy, secret
redaction, subprocess execution, and result modeling. It now preserves that
public surface while delegating each responsibility to a focused component.

New integrations may use :mod:`evidence_search` directly when they need to
inject a backend. Existing callers should continue importing ``fallback``;
the facade is intentionally lightweight and backward compatible.
"""

from __future__ import annotations

import shutil as shutil
import subprocess as subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from .evidence_search import ProcessRunner, search_knowledge, search_source
from .models import EvidenceSearchResult, FallbackResult
from .query_terms import extract_terms, has_explicit_locator

__all__ = [
    "EvidenceSearchResult",
    "FallbackResult",
    "extract_terms",
    "has_explicit_locator",
    "search",
    "search_docs",
]


def search_docs(
    root: str | Path,
    requirement: str,
    *,
    doc_targets: list[Path],
    max_results: int = 10,
    prefer_rg: bool = True,
    timeout_seconds: float | None = None,
    terms: Sequence[str] | None = None,
    deadline_monotonic: float | None = None,
) -> FallbackResult:
    """Search caller-selected canonical Markdown knowledge."""
    return search_knowledge(
        root,
        requirement,
        doc_targets=doc_targets,
        locate_executable=shutil.which,
        run_process=cast(ProcessRunner, subprocess.run),
        max_results=max_results,
        prefer_rg=prefer_rg,
        timeout_seconds=timeout_seconds,
        terms=terms,
        deadline_monotonic=deadline_monotonic,
    )


def search(
    root: str | Path,
    requirement: str,
    *,
    max_results: int = 20,
    prefer_rg: bool = True,
    timeout_seconds: float | None = None,
    extra_terms: Sequence[str] = (),
    path_prefixes: Sequence[str] = (),
    deadline_monotonic: float | None = None,
) -> FallbackResult:
    """Search current project source for bounded, redacted evidence."""
    return search_source(
        root,
        requirement,
        locate_executable=shutil.which,
        run_process=cast(ProcessRunner, subprocess.run),
        max_results=max_results,
        prefer_rg=prefer_rg,
        timeout_seconds=timeout_seconds,
        extra_terms=extra_terms,
        path_prefixes=path_prefixes,
        deadline_monotonic=deadline_monotonic,
    )
