"""Content-addressed compiler extraction cache."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from modus.artifacts_io import atomic_write_text
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.semantic_ir import SemanticFacts, canonical_json, parse_jsonl
from modus.machine.languages import PROVIDER_VERSIONS
from modus.paths import ModusPaths

from .process import _safe_name

COMPILER_CACHE_CONTRACT_VERSION = 1


def _facts_path(paths: ModusPaths, unit: BuildUnit) -> Path:
    """Return the per-unit cache location for one provider's persisted facts."""
    provider = PROVIDER_VERSIONS[unit.language].split("/", 1)[0]
    return paths.compiler_cache_dir / "facts" / provider / f"{_safe_name(unit.id)}.json"


def _load_facts(path: Path, *, context_digest: str) -> SemanticFacts | None:
    """Load a cached fact set, or None if absent, malformed, or context-stale.

    Any structural mismatch invalidates the whole cache entry rather than
    partially trusting it, so a corrupted file can never leak into a build.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, Mapping)
        or value.get("schemaVersion") != 1
        or value.get("contextDigest") != context_digest
        or value.get("contractVersion") != COMPILER_CACHE_CONTRACT_VERSION
    ):
        return None
    try:
        records = [
            *(dict(item, recordKind="node") for item in value.get("nodes", ())),
            *(dict(item, recordKind="edge") for item in value.get("edges", ())),
            *(dict(item, recordKind="diagnostic") for item in value.get("diagnostics", ())),
            *value.get("contexts", ()),
            *value.get("dispositions", ()),
            *value.get("coverageReceipts", ()),
        ]
        return parse_jsonl(canonical_json(item) for item in records)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _write_facts(path: Path, unit: BuildUnit, facts: SemanticFacts) -> None:
    """Persist one unit's fact set atomically, tagged with its cache contract."""
    value = facts.to_dict()
    value["contractVersion"] = COMPILER_CACHE_CONTRACT_VERSION
    value["contextDigest"] = unit.context_digest
    value["buildUnitId"] = unit.id
    value["providerVersion"] = PROVIDER_VERSIONS[unit.language]
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


__all__ = ["_facts_path", "_load_facts", "_write_facts"]
