"""Bounded parsing of untrusted host events.

Host payloads are an input boundary, not application data.  This module first
enforces a byte envelope and strict JSON semantics, then retains only the two
allowlisted values the runtime needs.  Callers can choose strict parsing for
diagnostics or the fail-open adapter required by lifecycle hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NoReturn, Protocol, TypeAlias

from .contracts import HostName, require_host

MAX_HOOK_INPUT_BYTES: Final = 64 * 1024
MAX_SESSION_ID_BYTES: Final = 4096

logger = logging.getLogger(__name__)


class HookInputReader(Protocol):
    """Minimal stream contract accepted from host adapters."""

    def read(self, size: int = -1) -> str | bytes | bytearray:
        """Read at most the requested input envelope from a host stream."""


@dataclass(frozen=True, slots=True)
class HookEvent:
    """Allowlisted metadata safe to retain after input parsing."""

    host: HostName
    session_key: str


HookInput: TypeAlias = Mapping[str, object] | str | bytes | bytearray | HookInputReader


def make_session_key(host: str, raw_session_id: str) -> str:
    """Return the canonical opaque key used by both hook and CLI bindings.

    The project path is intentionally not part of this digest: session binding
    files are already project-scoped.  Keeping one public derivation prevents a
    CLI binding from hashing a different representation than a later hook load.
    """

    contract = require_host(host)
    session_bytes = _bounded_session_id(raw_session_id)
    digest = hashlib.sha256(
        b"modus-hook-session\0" + contract.name.encode("ascii") + b"\0" + session_bytes
    ).hexdigest()
    return f"sha256:{digest}"


def parse_hook_event(source: HookInput, *, host: str) -> HookEvent | None:
    """Strictly parse a bounded event, returning ``None`` for other event types.

    Malformed input raises ``ValueError``.  Raw conversational and tool fields
    are validated only as part of the size/JSON envelope and are never copied to
    the returned event.
    """

    contract = require_host(host)
    payload = _load_payload(source)

    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or event_name not in contract.accepted_event_names:
        return None

    raw_session_id = payload.get("session_id")
    if not isinstance(raw_session_id, str):
        raise ValueError("钩子 session_id 必须为字符串")
    return HookEvent(
        host=contract.name,
        session_key=make_session_key(contract.name, raw_session_id),
    )


def read_hook_event(source: HookInput, *, host: str) -> HookEvent | None:
    """Fail open around :func:`parse_hook_event` for host lifecycle adapters."""

    try:
        return parse_hook_event(source, host=host)
    # NOCA:broad-except(宿主输入适配器保证失败放行，严格解析入口仍传播异常)
    except Exception:
        # Hook adapters must never make the host lifecycle depend on Modus.
        logger.debug("已忽略无效钩子事件", exc_info=True)
        return None


def _bounded_session_id(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("钩子 session_id 必须为非空字符串")
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) > MAX_SESSION_ID_BYTES:
        raise ValueError("钩子 session_id 超过标识长度上限")
    return encoded


def _load_payload(source: HookInput) -> Mapping[str, object]:
    if isinstance(source, Mapping):
        if not all(isinstance(key, str) for key in source):
            raise ValueError("钩子输入对象的键必须为字符串")
        raw = json.dumps(
            source,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise ValueError("钩子输入超过 64 KiB")
        return source

    raw_source: object
    if isinstance(source, (str, bytes, bytearray)):
        raw_source = source
    else:
        reader = getattr(source, "read", None)
        if not callable(reader):
            raise ValueError("钩子输入必须为对象、字节、文本或读取器")
        raw_source = reader(MAX_HOOK_INPUT_BYTES + 1)

    if isinstance(raw_source, str):
        raw = raw_source.encode("utf-8", errors="strict")
    elif isinstance(raw_source, (bytes, bytearray)):
        raw = bytes(raw_source)
    else:
        raise ValueError("钩子读取器返回了不支持的值")
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("钩子输入超过 64 KiB")

    decoded = raw.decode("utf-8", errors="strict")
    value = json.loads(
        decoded,
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("钩子输入必须解码为 JSON 对象")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("钩子输入包含重复对象键")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("钩子输入包含非有限 JSON 数值")
