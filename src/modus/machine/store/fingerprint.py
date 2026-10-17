"""Pure structural projection identity used by the extraction cache."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_VOLATILE_ITEM_KEYS = frozenset({"content_digest"})


def _stable_item(item: Any) -> Any:
    if isinstance(item, dict):
        return {
            key: _stable_item(value)
            for key, value in sorted(item.items())
            if key not in _VOLATILE_ITEM_KEYS
        }
    if isinstance(item, (list, tuple)):
        return [_stable_item(value) for value in item]
    return item


def extraction_projection_digest(file_result: dict[str, Any]) -> str:
    """Hash observed structure independently of cache and execution accounting.

    Content digests are tracked separately from the projected shape. Removing
    only these volatile fields keeps a cache hit equivalent to a fresh parse.
    """

    payload = _stable_item(
        {
            key: value
            for key, value in file_result.items()
            if key not in {"input_tokens", "output_tokens", "cacheStats", "elapsedSeconds"}
        }
    )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["extraction_projection_digest"]
