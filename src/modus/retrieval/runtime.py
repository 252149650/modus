"""Stable runtime API for project and workspace retrieval.

This module is intentionally a facade.  Intent analysis, route inference,
document stages, and delivery policy evolve independently behind it while CLI
and embedding integrations retain one small, predictable import surface.
"""

from . import pipeline
from .delivery import limit_result
from .pipeline import enrich_workspace_result, finish_context
from .query import analyze_query, select_workspace_members
from .routing import build_route_catalog

# Alpha-era private imports are retained as aliases so existing integrations and
# tests can migrate without coupling new code to the implementation module.
_anchored_source_refs = pipeline._anchored_source_refs
_applicable_constraints = pipeline._applicable_constraints
_coverage = pipeline._coverage
_entry_refs = pipeline._entry_refs
_finish = pipeline._finish
_knowledge_ref = pipeline._knowledge_ref
_live_source_refs = pipeline._live_source_refs
_need_matches = pipeline._need_matches
_render_context = pipeline._render_context
_route_records = pipeline._route_records
_settings = pipeline._settings
_source_in_scope = pipeline._source_in_scope
_source_ref = pipeline._source_ref
_source_type = pipeline._source_type
_route_catalog = build_route_catalog

__all__ = [
    "analyze_query",
    "enrich_workspace_result",
    "finish_context",
    "limit_result",
    "select_workspace_members",
]
