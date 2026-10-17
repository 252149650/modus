"""N6 Authoring Context Packet -- the least a task needs. Verb: package.

This stage assembles one intent-scoped, budget-bounded packet with coverage
receipts and precise expansion handles. It never requires the consumer to
exhaust machine records to trust the result.
"""

from __future__ import annotations

from .contracts import (
    AuthoringContextPacket,
    ContextRequest,
    ExpansionHandle,
    HandleKind,
    PacketBudget,
    PacketScope,
    TaskIntent,
)
from .lifecycle import package

__all__ = [
    "AuthoringContextPacket",
    "ContextRequest",
    "ExpansionHandle",
    "HandleKind",
    "PacketBudget",
    "PacketScope",
    "TaskIntent",
    "package",
]
