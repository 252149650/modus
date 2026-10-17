"""N4 Capability Model -- what can the system do? Verb: compose.

This stage walks resolved relations from public entries to terminal effects
and compresses them into bounded capability slices. It composes behaviour
evidence; it never decides formal domains or modules.
"""

from __future__ import annotations

from .contracts import (
    CapabilityModel,
    CapabilitySlice,
    EffectKind,
    PathStatus,
    PathStep,
    RepresentativePath,
    TerminalEffect,
    capability_identity,
)
from .lifecycle import ComposedCapabilities, compose

__all__ = [
    "CapabilityModel",
    "CapabilitySlice",
    "EffectKind",
    "PathStatus",
    "PathStep",
    "RepresentativePath",
    "TerminalEffect",
    "capability_identity",
    "ComposedCapabilities",
    "compose",
]
