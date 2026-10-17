"""Opaque, integrity-bound cursor codec for evidence queries."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import cast

from ..errors import ContractError, IntegrityError
from ..store.bundle import canonical_bytes
from ..store.contracts import (
    SCHEMA_VERSION,
    AnalysisBundle,
    EvidenceQuery,
    JsonObject,
)


# 游标绑定查询过滤条件及分析版本，不能跨结果集复用。
# 集合型条件排序去重，使语义相同的过滤生成相同摘要。
def _query_payload(bundle: AnalysisBundle, query: EvidenceQuery) -> JsonObject:
    return cast(
        JsonObject,
        {
            "schemaVersion": SCHEMA_VERSION,
            "bundleDigest": bundle.bundle_digest,
            "baselineDigest": bundle.baseline_digest,
            "view": query.view,
            "candidateIds": sorted(set(query.candidate_ids)),
            "capabilityIds": sorted(set(query.capability_ids)),
            "boundaryIds": sorted(set(query.boundary_ids)),
            "paths": sorted(set(query.paths)),
            "symbols": sorted(set(query.symbols)),
            "recordTypes": sorted(set(query.record_types)),
            "evidenceKinds": sorted(set(query.evidence_kinds)),
        },
    )


def _query_digest(bundle: AnalysisBundle, query: EvidenceQuery) -> str:
    return hashlib.sha256(canonical_bytes(_query_payload(bundle, query))).hexdigest()


# 校验和用于检测损坏与不一致，不是带密钥的身份认证。
# 位置使用目录键而非偏移量，保持稳定的增量遍历语义。
def encode_cursor(bundle: AnalysisBundle, query: EvidenceQuery, *, after_key: str) -> str:
    """Encode the last consumed catalog key with bundle and query commitments."""
    body: JsonObject = {
        "schemaVersion": SCHEMA_VERSION,
        "bundleDigest": bundle.bundle_digest,
        "baselineDigest": bundle.baseline_digest,
        "queryDigest": _query_digest(bundle, query),
        "afterKey": after_key,
    }
    checksum = hashlib.sha256(canonical_bytes(body)).hexdigest()
    envelope: JsonObject = {**body, "checksum": checksum}
    return (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )


# 先验证封套完整性，再逐项验证版本和查询归属。
# 失败直接抛出契约错误，不回退到第一页以掩盖重复读取。
# 没有游标表示首次读取；空 afterKey 则属于无效封套。
def decode_cursor(bundle: AnalysisBundle, query: EvidenceQuery) -> str:
    """Authenticate a cursor and return its last consumed catalog key."""
    if query.cursor is None:
        return ""
    raw = query.cursor
    try:
        padding = "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ContractError("machine.packet.cursor-invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("machine.packet.cursor-invalid")
    checksum = value.pop("checksum", None)
    expected = hashlib.sha256(canonical_bytes(value)).hexdigest()
    if checksum != expected:
        raise IntegrityError("machine.packet.cursor-checksum")
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("machine.packet.cursor-schema")
    if value.get("bundleDigest") != bundle.bundle_digest:
        raise ContractError("machine.packet.cursor-bundle-mismatch")
    if value.get("baselineDigest") != bundle.baseline_digest:
        raise ContractError("machine.packet.cursor-baseline-mismatch")
    if value.get("queryDigest") != _query_digest(bundle, query):
        raise ContractError("machine.packet.cursor-query-mismatch")
    after_key = value.get("afterKey")
    if not isinstance(after_key, str) or not after_key:
        raise ContractError("machine.packet.cursor-position")
    return after_key


__all__ = ["decode_cursor", "encode_cursor"]
