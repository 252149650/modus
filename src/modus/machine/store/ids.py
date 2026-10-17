"""Readable keys and collision-safe occurrence identities for Modus facts."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable


def normalize_id(value: str) -> str:
    """Readable legacy key; use occurrence_id for actual source identity."""
    tokens: list[str] = []
    word: list[str] = []
    for character in unicodedata.normalize("NFC", value):
        if character.isalnum():
            word.append(character.casefold())
        elif word:
            tokens.append("".join(word))
            word.clear()
    if word:
        tokens.append("".join(word))
    return "_".join(tokens)


def make_id(*parts: str) -> str:
    """Join non-empty *parts* into one readable normalized key."""
    return normalize_id("/".join(part for part in parts if part))


def occurrence_id(path: str, start: int, end: int, role: str, spelling: str = "") -> str:
    """Identity includes exact position and spelling, never a lossy search key."""
    payload = json.dumps(
        [path, start, end, role, spelling], ensure_ascii=False, separators=(",", ":")
    )
    return "occurrence:" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def candidate_set_identity(targets: Iterable[object]) -> str:
    """Return a stable identity for one normalized ambiguous target set."""
    normalized = sorted({str(target) for target in targets})
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"candidates:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"
