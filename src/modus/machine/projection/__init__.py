"""N5 Authoring Projections -- which decisions do facts support? Verb: project.

This stage aggregates capabilities into evidence-backed proposals and views.
It proposes; the author decides. It never emits an unbounded underlying graph
and never claims authored truth.
"""

from __future__ import annotations

from .contracts import (
    AuthoringProjection,
    ProjectionIntent,
    Proposal,
    ProposalDisposition,
)
from .lifecycle import ProjectionProducts, project

__all__ = [
    "AuthoringProjection",
    "Proposal",
    "ProposalDisposition",
    "ProjectionIntent",
    "ProjectionProducts",
    "project",
]
