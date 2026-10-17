"""Compose bounded project context from narrative and current source evidence.

The resolver is an application service: it validates one request, establishes
one shared budget, delegates evidence collection, and assembles the public JSON
contract. Indexes are optional navigation aids and never act as setup gates or
as authority over current source.

Initialization analysis is deliberately absent. This resolver answers from
canonical knowledge and current source alone.
"""

from __future__ import annotations

import hashlib
import time

from ..config import ModusConfig
from ..paths import ModusPaths
from ..retrieval.budget import RetrievalBudget
from .models import JsonObject
from .path_scope import PathOutsideRootError, relative_paths

__all__ = ["PathOutsideRootError", "relative_paths", "resolve_context"]

VALID_SCOPES = frozenset({"project-read"})
_DEFAULT_COLD_BUDGET_MS = 3_000
_DEFAULT_MAX_CONTEXT_CHARS = 12_000


def resolve_context(
    root: str,
    requirement: str,
    *,
    scope: str,
    explicit_paths: list[str] | None = None,
    purpose: str | None = None,
    budget: RetrievalBudget | None = None,
) -> JsonObject:
    """Resolve one request under a single cooperative time and output budget."""
    started = time.monotonic()
    _validate_request(requirement, scope)

    # These imports stay local so ordinary CLI startup does not load the
    # narrative retrieval pipeline before a context command actually needs it.
    from ..retrieval import runtime
    from ..retrieval.index import scan_index

    paths = ModusPaths.for_root(root)
    explicit = relative_paths(paths, explicit_paths)
    active_budget = budget or _default_budget(paths, started)
    index = scan_index(
        paths,
        kind="project",
        query=requirement,
        explicit_paths=explicit,
        deadline_monotonic=active_budget.deadline,
    )
    base = _initial_context(
        paths,
        requirement=requirement,
        scope=scope,
        explicit_paths=explicit,
        source_search_enabled=_source_search_enabled(paths),
    )
    result: JsonObject = runtime.finish_context(
        paths,
        base,
        kind="project",
        query_text=requirement,
        route={"kind": "project", "members": [], "domains": [], "modules": [], "paths": explicit},
        index=index,
        purpose=purpose,
        budget=active_budget,
    )
    # ``sourceSearchEnabled`` is orchestration state, not part of the public
    # response contract.
    result.pop("sourceSearchEnabled", None)
    result["retrieval"]["elapsedMs"] = active_budget.elapsed_ms
    if active_budget.exhausted:
        _record_budget_exhaustion(result)
    runtime.limit_result(result, max_chars=active_budget.max_chars)
    return result


def _validate_request(requirement: str, scope: str) -> None:
    if scope not in VALID_SCOPES:
        raise ValueError("scope 必须为 project-read")
    if not requirement.strip():
        raise ValueError("需要提供任务描述")


def _default_budget(paths: ModusPaths, started: float) -> RetrievalBudget:
    from ..retrieval.stages.settings import retrieval_settings

    settings = retrieval_settings(paths)
    duration_ms = float(settings.get("cold_budget_ms", _DEFAULT_COLD_BUDGET_MS))
    max_chars = int(settings.get("max_context_chars", _DEFAULT_MAX_CONTEXT_CHARS))
    return RetrievalBudget(started, started + duration_ms / 1_000, max_chars)


def _source_search_enabled(paths: ModusPaths) -> bool:
    try:
        config = ModusConfig.read_available(paths)
    except (OSError, ValueError):
        return True
    return config.ripgrep_fallback


def _initial_context(
    paths: ModusPaths,
    *,
    requirement: str,
    scope: str,
    explicit_paths: list[str],
    source_search_enabled: bool,
) -> JsonObject:
    source_files = [
        path
        for path in explicit_paths
        if (paths.root / path).is_file() and not path.startswith(("modus/", ".modus/"))
    ]
    return {
        "requestHash": hashlib.sha256(requirement.encode()).hexdigest(),
        "scope": scope,
        "kind": "project",
        "sourceFiles": source_files,
        "knowledgeFiles": [],
        "routeEvidence": [],
        "freshness": {"mode": "source"},
        "contextMarkdown": "",
        "explicitPaths": explicit_paths,
        "sourceSearchEnabled": source_search_enabled,
    }


def _record_budget_exhaustion(result: JsonObject) -> None:
    result["freshness"].update(budgetExhausted=True, mode="degraded")
    result["retrieval"].update(stage="degraded", stopReason="budget-exhausted")
    result["continuation"].update(available=True, reason="budget-exhausted")
    coverage_items = result["coverage"]["items"]
    if not any(item["need"] == "runtime-budget" for item in coverage_items):
        coverage_items.append({"need": "runtime-budget", "status": "partial", "evidenceIds": []})
