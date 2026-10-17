"""Compatibility facade for Markdown discovery, parsing, and link indexing.

The implementation lives in :mod:`.documents`.  Keeping this deliberately thin
module preserves the original import surface while making the package layout
communicate a clearer boundary: ``documents`` owns ingestion; callers consume
section records through this facade or the public functions below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...paths import ModusPaths
from . import documents as _documents
from .discovery import discover_markdown_files

INDEX_SCHEMA_VERSION = _documents.INDEX_SCHEMA_VERSION
MAX_DOCUMENT_BYTES = _documents.MAX_DOCUMENT_BYTES
MAX_SCAN_BYTES = _documents.MAX_SCAN_BYTES
MAX_INDEX_BYTES = _documents.MAX_INDEX_BYTES

markdown_files = discover_markdown_files
markdown_headings = _documents.markdown_headings
is_rule_evidence = _documents.is_rule_evidence


def scan_sections(
    paths: ModusPaths,
    targets: list[Path],
    *,
    deadline_monotonic: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], dict[str, str], bool, list[dict[str, Any]]]:
    """Return section records, links, input digests, partial state, and issues."""

    return _documents.scan_documents(
        paths,
        targets,
        deadline_monotonic=deadline_monotonic,
        max_document_bytes=MAX_DOCUMENT_BYTES,
        max_scan_bytes=MAX_SCAN_BYTES,
    ).as_legacy_tuple()


build_link_index = _documents.build_link_index

# Private compatibility names retained for downstream users of the alpha API.
_canonical_json = _documents.canonical_json
_classify = _documents.classify_document
_LABELED_LINK_RE = _documents._LABELED_LINK_RE
_links_product = build_link_index
_sections = scan_sections

__all__ = [
    "INDEX_SCHEMA_VERSION",
    "MAX_DOCUMENT_BYTES",
    "MAX_INDEX_BYTES",
    "MAX_SCAN_BYTES",
    "build_link_index",
    "is_rule_evidence",
    "markdown_files",
    "markdown_headings",
    "scan_sections",
]
