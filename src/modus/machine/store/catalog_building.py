"""Canonical record construction shared by immutable bundle catalog writers.

These helpers normalize JSON shapes and preserve stable record identities. They
consume supplied values only; lifecycle stages remain responsible for obtaining
and validating source evidence before a catalog is built.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

from modus.machine.store.contracts import SCHEMA_VERSION, JsonObject
from modus.machine.store.evidence_records import parse_record, stable_record_id


def _digest_path(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _as_objects(value: object) -> list[JsonObject]:
    return (
        [cast(JsonObject, row) for row in value if isinstance(row, dict)]
        if isinstance(value, list)
        else []
    )


def _as_strings(value: object) -> tuple[str, ...]:
    return (
        tuple(str(row) for row in value if isinstance(row, str) and row)
        if isinstance(value, list)
        else ()
    )


# 布尔值虽是整数子类，也不能作为有效源码行号。
def _line(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _integer(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# 证据位置携带源码摘要，使展示锚点能够绑定到具体输入版本。
def _witness(
    *,
    path: str,
    source_digest: str,
    symbol: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
) -> JsonObject:
    return {
        "path": path,
        "symbol": symbol,
        "startLine": start_line,
        "endLine": end_line,
        "sourceDigest": source_digest,
    }


# 记录创建后立即走公共解析契约，防止写入器产生读不回的条目。
# 未知置信度降为未解析，不能因扩展字段而自动提升可信等级。
def _positive_record(
    kind: str,
    *,
    identity: tuple[object, ...],
    candidate_ids: tuple[str, ...],
    resolution: str,
    source_layer: str,
    witness: JsonObject,
    details: JsonObject,
) -> JsonObject:
    value: JsonObject = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": kind,
        "recordId": stable_record_id(kind, *identity),
        "candidateIds": list(candidate_ids),
        "resolution": resolution
        if resolution in {"exact", "candidate", "heuristic", "unresolved"}
        else "unresolved",
        "sourceLayer": source_layer
        if source_layer in {"cst", "project-binding", "compiler", "reconciled"}
        else "cst",
        "witness": witness,
        "details": details,
    }
    parse_record(value)
    return value


def _source_digests(snapshot: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(row.get("path")): str(row.get("sha256"))
        for row in _as_objects(snapshot.get("sources"))
        if row.get("path") and row.get("sha256")
    }
