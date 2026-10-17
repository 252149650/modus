"""One resource policy for analysis; profiles never change semantic rules."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

from ..errors import ConfigurationError


@dataclass(frozen=True)
class ResourcePolicy:
    """Scheduling and memory envelope for one analysis run.

    A policy scales concurrency and budgets only; it never changes which
    facts are extracted or how they are resolved, so two runs with different
    policies converge on the same published evidence bundle.
    """

    profile: str = "auto"
    workers: int = 1
    memory_bytes: int = 512 * 1024 * 1024
    compiler_workers: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_POLICY: ContextVar[ResourcePolicy | None] = ContextVar("modus_resource_policy", default=None)


def current_policy() -> ResourcePolicy:
    """Return the policy installed by :func:`use_policy`, or conservative defaults."""
    return _POLICY.get() or ResourcePolicy()


@contextmanager
def use_policy(policy: ResourcePolicy) -> Iterator[ResourcePolicy]:
    """Install *policy* for the current context; restored on exit."""
    token = _POLICY.set(policy)
    try:
        yield policy
    finally:
        _POLICY.reset(token)


def resolve_policy(
    profile: str = "auto", *, workers: int | None = None, memory_budget: int | None = None
) -> ResourcePolicy:
    """Derive a concrete policy from a named *profile* and optional overrides.

    Concurrency is bounded by CPU count, the memory budget (256 MiB per
    worker), and the selected profile. Raises ``ValueError`` for an
    unknown profile, a budget below 64 MiB, or non-positive workers.
    """
    if profile not in {"auto", "low-memory", "balanced", "throughput"}:
        raise ConfigurationError("machine.runtime.unknown-resource-profile")
    try:
        available = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        available = 2 * 1024**3
    defaults = {
        "auto": min(available // 4, 4 * 1024**3),
        "low-memory": 512 * 1024**2,
        "balanced": 2 * 1024**3,
        "throughput": min(available // 2, 8 * 1024**3),
    }
    memory = memory_budget if memory_budget is not None else defaults[profile]
    if memory < 64 * 1024**2:
        raise ConfigurationError("machine.runtime.invalid-memory-budget")
    profile_caps = {
        "low-memory": 2,
        "balanced": 8,
        "auto": 16,
        "throughput": 32,
    }
    concurrency = (
        workers
        if workers is not None
        else min(
            os.cpu_count() or 1,
            max(1, memory // (256 * 1024**2)),
            profile_caps[profile],
        )
    )
    if concurrency < 1:
        raise ConfigurationError("machine.runtime.invalid-worker-count")
    return ResourcePolicy(profile, concurrency, memory, min(2, concurrency))
