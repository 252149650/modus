"""Isolate invalid optional view records before strict fact publication.

This boundary checks declared identities, endpoints and direct support. It does
not infer missing evidence or repair a record by guessing another symbol. A
reverse dependency worklist removes only records that lose all alternatives.
The bundle publisher later atomically seals the accepted projection.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from modus.machine.corpus.input_snapshot import valid_analysis_inputs
from modus.machine.errors import validate_code
from modus.machine.resolution.evidence_graph import iter_keyed_edges
from modus.machine.resolution.topology import TOPOLOGY_SCHEMA_VERSION

# Bounded receipt: at most this many rejection samples are kept verbatim.
_SAMPLE_LIMIT = 64
# Truncation bound for rejected metadata key names inside samples.
_METADATA_KEY_LIMIT = 160
# Required length and alphabet of a valid analysis-input content digest.
_INPUT_DIGEST_LENGTH = 64
_HEX_DIGITS = "0123456789abcdef"
# Container keys holding structured view rows rather than scalar metadata.
_STRUCTURAL_KEYS = {"nodes", "edges", "entries", "methodGraph"}
# Rule keys replaced by the normalized contextId during direct-support admission.
_CONTEXT_ALIAS_KEYS = {"context_id", "analysisContextId", "analysis_context_id"}


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _string(value: Any, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or "\0" in value:
        raise ValueError("machine.resolution.invalid-string")
    return value


def _rejection_summary(error: BaseException) -> str:
    """Keep canonical rejection codes, never exception text or rejected values."""
    summary = str(error)
    try:
        return validate_code(summary)
    except ValueError:
        return type(error).__name__


def _context(data: Mapping[str, Any]) -> str:
    return _string(
        data.get("analysis_context_id")
        or data.get("analysisContextId")
        or data.get("context_id")
        or data.get("contextId")
        or "",
        "context",
        empty=True,
    )


@dataclass
class _Record:
    view: str
    kind: str
    row: dict[str, Any]
    context: str
    endpoints: tuple[str, ...] = ()
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    declared_rules: bool = False


@dataclass
class _Accounting:
    """Mutable rejection bookkeeping shared by every quarantine phase.

    A single instance accumulates observed/reason counters, bounded samples,
    the unknown-total flag and the rejected-rule count across all helpers.
    """

    observed: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    samples: list[dict[str, Any]] = field(default_factory=list)
    unknown_total: bool = False
    rejected_rules: int = 0


def _reject(
    state: _Accounting, view: str, kind: str, identity: str, reason: str, **detail: Any
) -> None:
    """Count one rejection reason and retain a sample while under _SAMPLE_LIMIT."""
    state.reasons[reason] += 1
    if len(state.samples) < _SAMPLE_LIMIT:
        state.samples.append(
            {"view": view, "kind": kind, "id": identity, "reason": reason, **detail}
        )


def _sanitize_metadata(state: _Accounting, value: Any, view: str) -> dict[str, Any]:
    """Copy JSON-canonical metadata of one view container, skipping structural keys.

    Non-mapping containers mark the total as unknown; non-JSON items are
    rejected individually and omitted from the returned dict.
    """
    if not isinstance(value, Mapping):
        state.unknown_total = True
        _reject(state, view, "container", "", "machine.resolution.view-object-required")
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _STRUCTURAL_KEYS:
            continue
        try:
            _string(key, "machine.resolution.view-metadata-key")
            canonical(item)
            result[key] = item
        except (TypeError, ValueError, OverflowError, RecursionError):
            _reject(
                state,
                view,
                "metadata",
                str(key)[:_METADATA_KEY_LIMIT],
                "machine.resolution.view-metadata-not-json",
            )
    return result


def _core_facts(
    graph: Any, sources: Iterable[str], analysis_inputs: Mapping[str, Any]
) -> tuple[dict[str, str], set[str]]:
    """Index already-grounded facts by id (value is the analysis context, "" if none).

    Also returns the derivation ids already reserved by graph edges so view
    records cannot claim them again.
    """
    core: dict[str, str] = {"source:" + path: "" for path in sources}
    reserved_rules: set[str] = set()
    for identity, data in graph.nodes(data=True):
        core["node:" + identity] = _context(data) or "syntax"
    for _source, _target, identity, data in iter_keyed_edges(graph):
        core["edge:" + identity] = _context(data) or "syntax"
        for rule in data.get("derivations", ()):
            if isinstance(rule, Mapping) and isinstance(rule.get("id"), str):
                reserved_rules.add(rule["id"])
    if valid_analysis_inputs(analysis_inputs):
        for key, record in analysis_inputs["files"].items():
            expected = (
                record["path"] if record["scope"] == "repository" else "external:" + record["path"]
            )
            value = record["digest"]
            if (
                record["status"] == "present"
                and key == expected
                and len(value) == _INPUT_DIGEST_LENGTH
                and all(char in _HEX_DIGITS for char in value)
            ):
                core["input:" + key] = ""
    return core, reserved_rules


def _view_rows(
    state: _Accounting, container: Mapping[str, Any], key: str, view: str, kind: str
) -> Sequence[Any]:
    """Return the declared row sequence of one view section, counting observed rows.

    A non-array section marks the total as unknown and yields no rows.
    """
    value = container.get(key, ())
    if not isinstance(value, (list, tuple)):
        state.unknown_total = True
        _reject(state, view, kind, "", "machine.resolution.view-records-array-required")
        return ()
    state.observed[view + "." + kind] += len(value)
    return value


def _parse_record(view: str, kind: str, value: Any) -> tuple[str, _Record | None, str | None]:
    """Validate one raw row into (identity, record, None) or (partial identity, None, detail).

    The returned identity reflects parsing progress so rejection samples can
    still name the record when validation fails after the id was read.
    """
    identity = ""
    try:
        if not isinstance(value, Mapping):
            raise ValueError("machine.resolution.view-record-object-required")
        canonical(value)
        row = dict(value)
        identity = _string(row.get("id"), "machine.resolution.view-id")
        context = _context(row)
        endpoints: tuple[str, ...] = ()
        if kind == "edge":
            source = _string(row.get("from", row.get("source")), "machine.resolution.view-source")
            target = _string(row.get("to", row.get("target")), "machine.resolution.view-target")
            _string(row.get("relation", ""), "machine.resolution.view-relation", empty=True)
            endpoint_view = "method" if view == "contract" else view
            endpoints = (
                f"view:{endpoint_view}:node:{source}",
                f"view:{endpoint_view}:node:{target}",
            )
        rules = row.get("derivations", ())
        if not isinstance(rules, (list, tuple)):
            raise ValueError("machine.resolution.view-derivations-array-required")
        record = _Record(view, kind, row, context, endpoints, declared_rules=bool(rules))
        return identity, record, None
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        return identity, None, _rejection_summary(exc)


def _collect_records(
    state: _Accounting, raw: Mapping[str, Any], raw_method: Mapping[str, Any]
) -> tuple[dict[str, _Record], set[str], list[Any]]:
    """Parse all declared view rows into keyed records plus duplicates and raw entries.

    Returns (records keyed by "view:<view>:<kind>:<id>", duplicated keys, raw
    method entry rows). Duplicate identities keep no usable record.
    """
    records: dict[str, _Record] = {}
    duplicates: set[str] = set()
    entries: list[Any] = []
    for view, container in (("topology", raw), ("method", raw_method)):
        for kind, key in (("node", "nodes"), ("edge", "edges")):
            for value in _view_rows(state, container, key, view, kind):
                identity, record, error = _parse_record(view, kind, value)
                if record is None:
                    _reject(
                        state,
                        view,
                        kind,
                        identity,
                        "machine.resolution.view-invalid-record",
                        detail=error or "unknown-rejection",
                    )
                    continue
                fact = f"view:{view}:{kind}:{identity}"
                if fact in records or fact in duplicates:
                    duplicates.add(fact)
                    _reject(
                        state, view, kind, identity, "machine.resolution.view-duplicate-identity"
                    )
                    continue
                records[fact] = record
        if view == "method":
            entries = list(_view_rows(state, container, "entries", view, "entry"))
    return records, duplicates, entries


def _drop(
    state: _Accounting,
    records: dict[str, _Record],
    live: set[str],
    removed: deque[str],
    identity: str,
    reason: str,
    **detail: Any,
) -> None:
    """Quarantine one live record: reject it once and queue reverse propagation.

    Idempotent for identities that are no longer live.
    """
    if identity in live:
        live.remove(identity)
        item = records[identity]
        _reject(state, item.view, item.kind, item.row["id"], reason, **detail)
        removed.append(identity)


def _admit_rules(
    state: _Accounting,
    identity: str,
    item: _Record,
    live: set[str],
    contexts: dict[str, str],
    core: dict[str, str],
    reserved_rules: set[str],
    dependents: dict[str, dict[str, set[str]]],
    rule_owners: dict[str, list[str]],
) -> None:
    """Normalize each declared derivation of one record into item.rules.

    Invalid rules are rejected in place (counted in rejected_rules); admitted
    rules register reverse premise links and their ownership claims.
    """
    for rule in item.row.get("derivations", ()):
        try:
            if not isinstance(rule, Mapping):
                raise ValueError("machine.resolution.rule-object-required")
            values = rule.get("premises")
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError("machine.resolution.rule-premises-required")
            premises = sorted(
                {_string(value, "machine.resolution.view-premise") for value in values}
            )
            if any(value not in core and value not in live for value in premises):
                raise ValueError("machine.resolution.rule-premise-unavailable")
            producer = _string(rule.get("producer", "modus"), "machine.resolution.view-producer")
            rule_name = _string(
                rule.get("rule", "explicit-support"), "machine.resolution.view-rule"
            )
            context = _context(rule)
            if item.view == "contract":
                if not set(item.endpoints).issubset(premises) or not any(
                    value.startswith(("source:", "input:")) for value in premises
                ):
                    raise ValueError("machine.resolution.rule-contract-deployment-required")
                rule_name, context = "machine.resolution.explicit-contract", ""
            elif (
                len(
                    {
                        value
                        for value in [
                            item.context,
                            context,
                            *(contexts[premise] for premise in premises),
                        ]
                        if value
                    }
                )
                > 1
            ):
                raise ValueError("machine.resolution.rule-context-mismatch")
            rule_id = _string(
                rule.get("id")
                or "derivation:" + digest([identity, premises, producer, rule_name, context]),
                "machine.resolution.view-derivation-id",
            )
            if rule_id in reserved_rules or rule_id in item.rules:
                raise ValueError("machine.resolution.rule-duplicate-identity")
            normalized = {
                key: value for key, value in rule.items() if key not in _CONTEXT_ALIAS_KEYS
            }
            normalized.update(
                id=rule_id,
                premises=premises,
                producer=producer,
                rule=rule_name,
                contextId=context,
            )
            item.rules[rule_id] = normalized
            rule_owners[rule_id].append(identity)
            for premise in premises:
                dependents[premise].setdefault(identity, set()).add(rule_id)
        except (TypeError, ValueError) as exc:
            state.rejected_rules += 1
            _reject(
                state,
                item.view,
                "derivation",
                item.row["id"],
                "machine.resolution.view-invalid-direct-support",
                detail=_rejection_summary(exc),
            )


def _admit_records(
    state: _Accounting,
    records: dict[str, _Record],
    live: set[str],
    removed: deque[str],
    contexts: dict[str, str],
    core: dict[str, str],
    reserved_rules: set[str],
) -> tuple[dict[str, dict[str, set[str]]], dict[str, list[str]]]:
    """Check endpoints, context agreement and direct support for every live record.

    Returns the reverse-link indexes: dependents maps a fact id to the records
    (and rule ids) relying on it; rule_owners maps a rule id to its claimants.
    """
    dependents: dict[str, dict[str, set[str]]] = defaultdict(dict)
    rule_owners: dict[str, list[str]] = defaultdict(list)
    for identity, item in records.items():
        if identity not in live:
            continue
        if any(endpoint not in live for endpoint in item.endpoints):
            _drop(
                state,
                records,
                live,
                removed,
                identity,
                "machine.resolution.view-endpoint-unavailable",
            )
            continue
        endpoint_contexts = [contexts[endpoint] for endpoint in item.endpoints]
        if item.view == "contract":
            if endpoint_contexts != [item.row["sourceContextId"], item.row["targetContextId"]]:
                _drop(
                    state,
                    records,
                    live,
                    removed,
                    identity,
                    "machine.resolution.view-contract-context-mismatch",
                )
                continue
        elif len({context for context in [item.context, *endpoint_contexts] if context}) > 1:
            _drop(
                state,
                records,
                live,
                removed,
                identity,
                "machine.resolution.view-endpoint-context-mismatch",
            )
            continue
        for endpoint in item.endpoints:
            dependents[endpoint].setdefault(identity, set())
        implicit = "node:" + str(item.row.get("symbolId") or item.row["id"])
        if item.kind == "node" and not item.declared_rules and implicit in core:
            if item.context and core[implicit] and item.context != core[implicit]:
                _drop(
                    state,
                    records,
                    live,
                    removed,
                    identity,
                    "machine.resolution.view-node-context-mismatch",
                )
                continue
        _admit_rules(
            state, identity, item, live, contexts, core, reserved_rules, dependents, rule_owners
        )
        if item.declared_rules and not item.rules:
            _drop(
                state,
                records,
                live,
                removed,
                identity,
                "machine.resolution.view-no-valid-alternative",
            )
    return dependents, rule_owners


def _prune_shared_rules(
    state: _Accounting,
    records: dict[str, _Record],
    live: set[str],
    removed: deque[str],
    rule_owners: dict[str, list[str]],
) -> None:
    """Revoke every copy of a derivation id claimed by more than one record.

    Records left without any rule are dropped as having no valid alternative.
    """
    for rule_id, owners in rule_owners.items():
        if len(owners) <= 1:
            continue
        for identity in owners:
            item = records[identity]
            item.rules.pop(rule_id, None)
            state.rejected_rules += 1
            _reject(
                state,
                item.view,
                "derivation",
                item.row["id"],
                "machine.resolution.rule-duplicate-identity",
            )
            if not item.rules:
                _drop(
                    state,
                    records,
                    live,
                    removed,
                    identity,
                    "machine.resolution.view-no-valid-alternative",
                )


def _propagate_removals(
    state: _Accounting,
    records: dict[str, _Record],
    live: set[str],
    removed: deque[str],
    dependents: dict[str, dict[str, set[str]]],
) -> None:
    """Drain the removal worklist, revoking endpoint and premise support transitively."""
    # Each failed fact visits its reverse links once. This propagates missing
    # endpoint and AND-premise support without rebuilding the full view set.
    while removed:
        missing = removed.popleft()
        for identity, affected_rules in dependents.get(missing, {}).items():
            if identity not in live:
                continue
            item = records[identity]
            if missing in item.endpoints:
                _drop(
                    state,
                    records,
                    live,
                    removed,
                    identity,
                    "machine.resolution.view-endpoint-quarantined",
                )
                continue
            for rule_id in affected_rules:
                if rule_id in item.rules:
                    del item.rules[rule_id]
                    state.rejected_rules += 1
                    _reject(
                        state,
                        item.view,
                        "derivation",
                        item.row["id"],
                        "machine.resolution.view-support-quarantined",
                        premise=missing,
                    )
            if item.declared_rules and not item.rules:
                _drop(
                    state,
                    records,
                    live,
                    removed,
                    identity,
                    "machine.resolution.view-no-valid-alternative",
                )


def _project_output(
    records: dict[str, _Record],
    live: set[str],
    topology: dict[str, Any],
    method: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], Counter[str]]:
    """Materialize surviving rows into per-view node/edge arrays, counting retention.

    Mutates topology and method in place (fresh nodes/edges arrays) and returns
    the full output mapping plus the retained counter keyed by "<view>.<kind>".
    """
    retained: Counter[str] = Counter()
    output: dict[str, dict[str, Any]] = {"topology": topology, "method": method}
    for value in output.values():
        value.update(nodes=[], edges=[])
    for identity, item in records.items():
        if identity not in live:
            continue
        row = item.row
        if item.declared_rules:
            row = {**row, "derivations": list(item.rules.values())}
        output[item.view][item.kind + "s"].append(row)
        retained[item.view + "." + item.kind] += 1
    return output, retained


def _admit_entries(
    state: _Accounting,
    entries: list[Any],
    live: set[str],
    contexts: dict[str, str],
    method: dict[str, Any],
    retained: Counter[str],
) -> None:
    """Publish method entries whose endpoints survived and whose contexts agree.

    Writes accepted rows into method["entries"]; each failure is rejected with
    a truncated detail string.
    """
    method["entries"] = []
    entry_ids: set[str] = set()
    for value in entries:
        try:
            if not isinstance(value, Mapping):
                raise ValueError("machine.resolution.entry-object-required")
            canonical(value)
            entry = _string(value.get("entryId"), "machine.resolution.view-entry")
            symbol = _string(value.get("symbolId"), "machine.resolution.view-entry-symbol")
            entry_endpoints = ["view:method:node:" + entry, "view:method:node:" + symbol]
            if any(endpoint not in live for endpoint in entry_endpoints):
                raise ValueError("machine.resolution.entry-endpoint-unavailable")
            if entry in entry_ids:
                raise ValueError("machine.resolution.entry-duplicate-identity")
            if (
                len(
                    {
                        context
                        for context in [
                            _context(value),
                            *(contexts[endpoint] for endpoint in entry_endpoints),
                        ]
                        if context
                    }
                )
                > 1
            ):
                raise ValueError("machine.resolution.entry-context-mismatch")
            method["entries"].append(dict(value))
            entry_ids.add(entry)
            retained["method.entry"] += 1
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            _reject(
                state,
                "method",
                "entry",
                "",
                "machine.resolution.view-invalid-entry",
                detail=_rejection_summary(exc),
            )


def _build_receipt(state: _Accounting, retained: Counter[str]) -> dict[str, Any]:
    """Assemble the deterministic, count-complete quarantine receipt.

    Keys are sorted for stable output; quarantined = observed - retained.
    """
    return {
        "schemaVersion": 1,
        "scope": "declared-derived-view-records",
        "status": "partial" if state.reasons else "complete",
        "observed": dict(sorted(state.observed.items())),
        "retained": {key: retained[key] for key in sorted(state.observed)},
        "quarantined": {key: state.observed[key] - retained[key] for key in sorted(state.observed)},
        "rejectedRules": state.rejected_rules,
        "unknownTotal": state.unknown_total,
        "reasonCounts": dict(sorted(state.reasons.items())),
        "sample": state.samples,
        "sampleOmitted": sum(state.reasons.values()) - len(state.samples),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return *value* when it is a mapping, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def _apply_stats(topology: dict[str, Any], method: dict[str, Any]) -> None:
    """Refresh published counts from surviving arrays."""
    stats = _mapping(topology.get("stats"))
    topology["stats"] = {
        **stats,
        "nodes": len(topology["nodes"]),
        "edges": len(topology["edges"]),
        "methodNodes": len(method["nodes"]),
        "methodEdges": len(method["edges"]),
        "exactEdges": sum(row.get("resolution") == "exact" for row in topology["edges"]),
        "unresolvedEdges": sum(row.get("resolution") == "unresolved" for row in topology["edges"]),
    }


def _mark_incomplete_coverage(topology: dict[str, Any], method: dict[str, Any]) -> None:
    """Downgrade both coverage claims once anything was quarantined; totals unknown."""
    for value in (topology, method):
        old = _mapping(value.get("coverage"))
        value["coverage"] = {
            **old,
            "complete": False,
            "reason": "machine.resolution.view-quarantined",
            "unknownTotal": True,
        }


def quarantine_topology(
    proposed: Any,
    graph: Any,
    *,
    sources: Iterable[str],
    analysis_inputs: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the usable projection and bounded, count-complete rejection receipt."""
    state = _Accounting()
    topology = _sanitize_metadata(state, proposed, "topology")
    topology.setdefault("schemaVersion", TOPOLOGY_SCHEMA_VERSION)
    raw: Mapping[str, Any] = proposed if isinstance(proposed, Mapping) else {}
    method = _sanitize_metadata(state, raw.get("methodGraph", {}), "method")
    raw_method = _mapping(raw.get("methodGraph"))
    core, reserved_rules = _core_facts(graph, sources, analysis_inputs)
    records, duplicates, entries = _collect_records(state, raw, raw_method)
    live = set(records) - duplicates
    contexts = {**core, **{identity: item.context for identity, item in records.items()}}
    removed: deque[str] = deque(duplicates)
    dependents, rule_owners = _admit_records(
        state, records, live, removed, contexts, core, reserved_rules
    )
    _prune_shared_rules(state, records, live, removed, rule_owners)
    _propagate_removals(state, records, live, removed, dependents)
    _output, retained = _project_output(records, live, topology, method)
    _admit_entries(state, entries, live, contexts, method, retained)
    topology["methodGraph"] = method
    receipt = _build_receipt(state, retained)
    topology["quarantine"] = receipt
    _apply_stats(topology, method)
    if state.reasons:
        _mark_incomplete_coverage(topology, method)
    return topology, receipt
