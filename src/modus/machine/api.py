"""Stable consumer façade for repository analysis and evidence access.

This module owns the public application boundary.  Lifecycle stages remain
available from their packages for maintainers, while consumers receive four
purpose-named operations and never need to know storage paths.
"""

from __future__ import annotations

from pathlib import Path

from modus.paths import ModusPaths

from .packet.context_builder import package_context
from .packet.contracts import AuthoringContextPacket, ContextRequest
from .packet.query import query_evidence
from .pipeline import build_pipeline
from .progress import Progress
from .store.bundle import open_bundle
from .store.contracts import (
    AnalysisBundle,
    AnalysisIdentity,
    AnalysisOptions,
    AnalysisReceipt,
)


def analyze_repository(
    root: str | Path,
    options: AnalysisOptions | None = None,
    *,
    progress: Progress | None = None,
) -> AnalysisReceipt:
    """Analyze one repository and publish an immutable evidence bundle."""

    return build_pipeline(root, options or AnalysisOptions(), progress=progress)


def open_analysis(root: str | Path, identity: AnalysisIdentity) -> AnalysisBundle:
    """Open an explicitly identified analysis after integrity verification."""

    return open_bundle(ModusPaths.for_root(root), identity)


def build_authoring_context(
    bundle: AnalysisBundle,
    request: ContextRequest,
) -> AuthoringContextPacket:
    """Build one bounded context packet for a single authoring decision."""

    return package_context(bundle, request)


__all__ = [
    "analyze_repository",
    "build_authoring_context",
    "open_analysis",
    "query_evidence",
]
