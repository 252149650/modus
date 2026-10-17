"""Canonical host contracts shared by hook installation and execution.

The hooks package has two producers of host-facing output: ``bootstrap`` emits
installation-time fallback payloads, while ``runtime`` emits live session
context.  Keeping their wire formats in one registry makes support for a host
an explicit capability instead of a collection of conditionals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal, TypeAlias, cast

HostName: TypeAlias = Literal["claude", "cursor", "codex", "codebuddy"]
ContextPayload: TypeAlias = str | dict[str, object]
CODEX_ADDITIONAL_CONTEXT_LIMIT: Final = 16_384


class ContextEnvelope(Enum):
    """Wire-level shapes used by supported session-start APIs."""

    PLAIN_TEXT = "plain-text"
    ADDITIONAL_CONTEXT = "additional-context"
    ADDITIONAL_CONTEXT_SNAKE_CASE = "additional-context-snake-case"
    HOOK_SPECIFIC_OUTPUT = "hook-specific-output"


@dataclass(frozen=True, slots=True)
class HostContract:
    """Everything shared code needs to know about one coding host."""

    name: HostName
    accepted_event_names: frozenset[str]
    static_envelope: ContextEnvelope
    runtime_envelope: ContextEnvelope

    def static_payload(self, context: str) -> ContextPayload:
        """Build the host's dependency-free installation fallback payload."""

        return _context_payload(context, self.static_envelope)

    def runtime_payload(self, context: str) -> ContextPayload:
        """Build the host's live runtime payload."""

        return _context_payload(context, self.runtime_envelope)


SESSION_START_EVENT_NAMES: Final = frozenset({"SessionStart", "sessionStart"})

# Order is public and stable: generated launchers use it for deterministic output.
SUPPORTED_HOSTS: Final[tuple[HostName, ...]] = ("claude", "cursor", "codex", "codebuddy")

_HOST_CONTRACTS: dict[HostName, HostContract] = {
    "claude": HostContract(
        name="claude",
        accepted_event_names=SESSION_START_EVENT_NAMES,
        static_envelope=ContextEnvelope.HOOK_SPECIFIC_OUTPUT,
        runtime_envelope=ContextEnvelope.HOOK_SPECIFIC_OUTPUT,
    ),
    "cursor": HostContract(
        name="cursor",
        accepted_event_names=SESSION_START_EVENT_NAMES,
        static_envelope=ContextEnvelope.ADDITIONAL_CONTEXT_SNAKE_CASE,
        runtime_envelope=ContextEnvelope.PLAIN_TEXT,
    ),
    "codex": HostContract(
        name="codex",
        accepted_event_names=SESSION_START_EVENT_NAMES,
        static_envelope=ContextEnvelope.ADDITIONAL_CONTEXT,
        runtime_envelope=ContextEnvelope.HOOK_SPECIFIC_OUTPUT,
    ),
    "codebuddy": HostContract(
        name="codebuddy",
        accepted_event_names=SESSION_START_EVENT_NAMES,
        static_envelope=ContextEnvelope.HOOK_SPECIFIC_OUTPUT,
        runtime_envelope=ContextEnvelope.PLAIN_TEXT,
    ),
}

HOST_CONTRACTS: Final[Mapping[HostName, HostContract]] = MappingProxyType(_HOST_CONTRACTS)


def require_host(value: object) -> HostContract:
    """Return the normalized contract for ``value`` or raise ``ValueError``."""

    if not isinstance(value, str):
        raise ValueError("钩子宿主必须为字符串")
    normalized = value.strip().lower()
    if normalized not in HOST_CONTRACTS:
        raise ValueError(f"不支持的钩子宿主：{normalized or '<empty>'}")
    return HOST_CONTRACTS[cast(HostName, normalized)]


def _context_payload(context: str, envelope: ContextEnvelope) -> ContextPayload:
    if envelope is ContextEnvelope.PLAIN_TEXT:
        return context
    if envelope is ContextEnvelope.ADDITIONAL_CONTEXT:
        return {"additionalContext": context}
    if envelope is ContextEnvelope.ADDITIONAL_CONTEXT_SNAKE_CASE:
        return {"additional_context": context}
    if envelope is ContextEnvelope.HOOK_SPECIFIC_OUTPUT:
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
    raise AssertionError(f"未处理的上下文封装：{envelope!r}")
