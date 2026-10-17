"""Signature-based merge semantics for shared platform hook containers.

Platform hook files are shared infrastructure, not Modus-owned documents.  A
merge therefore preserves unknown keys and foreign hook entries; uninstall
removes only entries carrying :data:`MODUS_HOOK_SIGNATURE`. Malformed content
is backed up before install-time recovery and left untouched during uninstall.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

MODUS_HOOK_SIGNATURE: Final = ".modus/hooks/run-hook.cmd"

# Backwards-compatible name used by older integrations.
SIGNATURE: Final = MODUS_HOOK_SIGNATURE


class HookConfigurationError(ValueError):
    """A generated hook configuration does not satisfy the merge contract."""


class HookConfigRecoveryWarning(UserWarning):
    """A malformed user hook file was backed up before Modus recovered it."""


def _contains_modus_signature(entry: object) -> bool:
    try:
        serialized = json.dumps(entry, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return MODUS_HOOK_SIGNATURE in serialized


def _dump_json_object(data: Mapping[str, object]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _backup_unusable_config(path: Path, reason: str) -> Path:
    """Snapshot an unusable container before an install-time recovery write."""

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.modus-corrupt-{stamp}.bak")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.modus-corrupt-{stamp}.{counter}.bak")
        counter += 1
    backup.write_bytes(path.read_bytes())
    warnings.warn(
        f"钩子配置 {path} {reason}；内容已备份到 {backup.name}，再写入 Modus 钩子（保留原内容）。",
        HookConfigRecoveryWarning,
        stacklevel=3,
    )
    return backup


def _load_json_object(path: Path) -> dict[str, object]:
    """Load a shared JSON object, recovering unusable install-time content."""

    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _backup_unusable_config(path, "不是有效 UTF-8")
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _backup_unusable_config(path, "不是有效 JSON")
        return {}
    if not isinstance(data, dict):
        _backup_unusable_config(path, "不是 JSON 对象")
        return {}
    return data


def _generated_hook_events(config: Mapping[str, object]) -> list[tuple[str, list[object]]]:
    hooks = config.get("hooks", {})
    if not isinstance(hooks, Mapping):
        raise HookConfigurationError("生成的钩子配置中 hooks 字段必须为对象")
    events: list[tuple[str, list[object]]] = []
    for event_name, entries in hooks.items():
        if not isinstance(event_name, str) or not isinstance(entries, list):
            raise HookConfigurationError("生成的钩子事件必须将字符串名称映射到条目列表")
        events.append((event_name, entries))
    return events


def merge_hook(path: Path, config: Mapping[str, object]) -> str:
    """Merge a generated Modus hook into ``path`` and return written JSON.

    Existing top-level keys win over generated defaults. Within each configured
    event, foreign entries retain their order and the previous Modus entry is
    replaced, making the operation deterministic and idempotent.
    """

    generated_events = _generated_hook_events(config)
    existing = _load_json_object(path)

    for key, value in config.items():
        if key != "hooks":
            existing.setdefault(key, value)

    raw_existing_hooks = existing.get("hooks")
    recovery_reasons: list[str] = []
    if isinstance(raw_existing_hooks, dict):
        existing_hooks = raw_existing_hooks
    else:
        if raw_existing_hooks is not None:
            recovery_reasons.append("has a non-object 'hooks' field")
        existing_hooks = {}

    for event_name, _generated_entries in generated_events:
        current_entries = existing_hooks.get(event_name)
        if current_entries is not None and not isinstance(current_entries, list):
            recovery_reasons.append(f"的钩子事件不是列表：{event_name!r}")

    if recovery_reasons:
        _backup_unusable_config(path, "; ".join(recovery_reasons))

    for event_name, generated_entries in generated_events:
        raw_current_entries = existing_hooks.get(event_name)
        current_entries = raw_current_entries if isinstance(raw_current_entries, list) else []
        retained_entries = [
            entry for entry in current_entries if not _contains_modus_signature(entry)
        ]
        existing_hooks[event_name] = [*retained_entries, *generated_entries]
    existing["hooks"] = existing_hooks

    content = _dump_json_object(existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return content


def unmerge_hook(path: Path) -> None:
    """Remove only Modus-signed hook entries and preserve unreadable containers."""

    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
        data = {} if not raw.strip() else json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        warnings.warn(
            f"钩子配置 {path} 无法读取为 JSON 对象；卸载时已保留原文件。",
            stacklevel=2,
        )
        return
    if not isinstance(data, dict):
        warnings.warn(
            f"钩子配置 {path} 不是 JSON 对象; 卸载时已保留原文件。",
            stacklevel=2,
        )
        return

    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks):
            entries = hooks[event_name]
            if not isinstance(entries, list):
                continue
            retained_entries = [entry for entry in entries if not _contains_modus_signature(entry)]
            if retained_entries:
                hooks[event_name] = retained_entries
            else:
                del hooks[event_name]
        if not hooks:
            data.pop("hooks", None)

    # ``version`` is the only scalar injected by current hook renderers. A file
    # containing only that generated default has no user-visible configuration.
    if not data or set(data) <= {"version"}:
        path.unlink()
    else:
        path.write_text(_dump_json_object(data), encoding="utf-8", newline="")
