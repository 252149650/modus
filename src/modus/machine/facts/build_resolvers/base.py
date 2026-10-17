"""State contract supplied by the build-context orchestrator."""

from __future__ import annotations

from modus.config import ModusConfig
from modus.machine.corpus.source_inventory import SourceInventory
from modus.machine.facts.build_contracts import Toolchain
from modus.paths import ModusPaths


class ResolverContext:
    """Shared state supplied by BuildContextResolver to language strategies.

    Strategies consume one inventory and may refine the detected toolchains.
    The orchestrator owns initialization and decides whether discovery may
    materialize optional provider inputs.
    """

    paths: ModusPaths
    config: ModusConfig
    inventory: SourceInventory
    materialize: bool
    toolchains: tuple[Toolchain, ...]
    _tools: dict[str, Toolchain]


__all__ = ["ResolverContext"]
