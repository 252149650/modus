"""Shared source-local context for framework observation phases.

Byte spans, lexical bindings and stable node identities have one owner here.
The context aliases the caller's node and edge lists; phases append observations
without replacing parser facts or inventing cross-file runtime bindings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from modus.machine.corpus.source_snapshot import SourcePath
from modus.machine.facts.value_summary import SourceValueIndex
from modus.machine.store.ids import make_id

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tree_sitter import Node


# Upper bound applied to stored node signatures (source excerpt length in characters).
_SIGNATURE_LIMIT = 500

_NUXT_AUTO_IMPORTS = {
    "$fetch",
    "useFetch",
    "useAsyncData",
    "ref",
    "computed",
    "reactive",
    "watch",
    "watchEffect",
    "useState",
    "defineEventHandler",
}

_VUE_REACTIVITY_EXPORTS = {"ref", "reactive", "computed", "watch", "watchEffect"}


def _walk(node: Node) -> Iterator[Node]:
    """Yield named nodes in depth-first pre-order, matching tree-sitter source order."""
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.named_children))


def _import_origins(root: Node, raw: bytes) -> dict[str, tuple[str, str]]:
    """Observe imports directly; package names alone do not bind identifiers."""
    result = {}

    def text(node):
        return raw[node.start_byte : node.end_byte].decode() if node else ""

    for declaration in root.named_children:
        if declaration.type != "import_statement":
            continue
        source = declaration.child_by_field_name("source")
        specifier = text(source).strip("\"'")
        clause = next(
            (child for child in declaration.named_children if child.type == "import_clause"), None
        )
        if not specifier or clause is None:
            continue
        for child in clause.named_children:
            if child.type == "identifier":
                result[text(child)] = (specifier, "default")
            elif child.type == "namespace_import":
                name = next(
                    (part for part in child.named_children if part.type == "identifier"), None
                )
                if name:
                    result[text(name)] = (specifier, "*")
            elif child.type == "named_imports":
                for item in child.named_children:
                    name = item.child_by_field_name("name")
                    alias = item.child_by_field_name("alias")
                    if name:
                        result[text(alias or name)] = (specifier, text(name))
    return result


@dataclass
class _Extraction:
    """Shared per-file extraction state.

    ``nodes`` and ``edges`` alias the lists inside ``result`` so appends publish
    directly; accumulator maps are filled by earlier phases and read by later ones.
    """

    path: SourcePath
    raw: bytes
    prefix: str
    file_id: str
    runtime: str
    framework: str
    origins: dict[str, tuple[str, str]]
    values: SourceValueIndex
    result: dict
    nodes: list[dict]
    edges: list[dict]
    # (start_byte, end_byte, node_id) spans of function-like declarations.
    declarations: list[tuple[int, int, str]] = field(default_factory=list)
    # name -> [(scope_start, scope_end, declaration_offset)] lexical bindings.
    bindings: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)
    # (name, declaration_offset) -> node id for states, setters and handlers.
    states: dict[tuple[str, int], str] = field(default_factory=dict)
    setters: dict[tuple[str, int], str] = field(default_factory=dict)
    handlers: dict[tuple[str, int], str] = field(default_factory=dict)
    # Byte spans of state declarator name patterns, excluded from read edges.
    state_offsets: set[tuple[int, int]] = field(default_factory=set)


def _expression(raw: bytes, syntax: Node | None) -> str:
    """Decode the exact original-source slice of a node; empty string for None."""
    return raw[syntax.start_byte : syntax.end_byte].decode() if syntax else ""


def _owner_at(ctx: _Extraction, offset: int) -> str:
    """Return the id of the smallest declaration span covering offset, else the file node."""
    choices = [
        (end - start, node_id) for start, end, node_id in ctx.declarations if start <= offset <= end
    ]
    return min(choices)[1] if choices else ctx.file_id


def _add_node(ctx: _Extraction, kind: str, label: str, start: int, end: int, **extra) -> str:
    """Append an observation node for the raw byte span [start, end] and return its id."""
    node_id = make_id(ctx.prefix, "frontend", kind, str(start))
    line = ctx.raw[:start].count(b"\n") + 1
    ctx.nodes.append(
        {
            "id": node_id,
            "label": label,
            "kind": kind,
            "file_type": "code",
            "source_file": str(ctx.path),
            "source_location": f"L{line}",
            "source_end_line": ctx.raw[:end].count(b"\n") + 1,
            "signature": ctx.raw[start:end].decode(errors="replace")[:_SIGNATURE_LIMIT],
            "runtimeBoundary": ctx.runtime,
            "analysis_completeness": "partial",
            **extra,
        }
    )
    return node_id


def _link(
    ctx: _Extraction, source: str, target: str, relation: str, offset: int, *, exact: bool = True
) -> None:
    """Append an edge located at the line containing the raw byte offset."""
    ctx.edges.append(
        {
            "source": source,
            "target": target,
            "relation": relation,
            "source_file": str(ctx.path),
            "source_location": f"L{ctx.raw[:offset].count(bytes([10])) + 1}",
            "resolution": "exact" if exact else "candidate",
            "binding_kind": "declared-exact" if exact else "runtime-candidate",
            "confidence": "EXTRACTED" if exact else "INFERRED",
        }
    )


def _lexical_scope(raw: bytes, node: Node) -> tuple[int, int]:
    """Byte span of the enclosing statement block, or the whole file at program scope."""
    parent = node.parent
    while parent and parent.type not in {"program", "statement_block"}:
        parent = parent.parent
    return (
        (parent.start_byte, parent.end_byte)
        if parent and parent.type != "program"
        else (0, len(raw))
    )


def _names_in(raw: bytes, pattern: Node | None) -> list[str]:
    """List identifiers introduced by a binding pattern, including destructuring shorthand."""
    return (
        [
            _expression(raw, child)
            for child in _walk(pattern)
            if child.type in {"identifier", "shorthand_property_identifier_pattern"}
        ]
        if pattern
        else []
    )


def _bound(
    ctx: _Extraction, table: dict[tuple[str, int], str], name: str, offset: int
) -> str | None:
    """Look up name in table via the innermost lexical binding visible at offset."""
    candidates = [
        (end - start, declaration)
        for start, end, declaration in ctx.bindings.get(name, [])
        if start <= offset <= end
    ]
    return table.get((name, min(candidates)[1])) if candidates else None


def _origin(ctx: _Extraction, callee: str, offset: int) -> tuple[str, str] | None:
    """Resolve a callee to (package, export), honoring lexical shadows and auto-imports."""
    root_name, _, member = callee.partition(".")
    if any(start <= offset <= end for start, end, _decl in ctx.bindings.get(root_name, ())):
        return None
    imported = ctx.origins.get(root_name)
    if imported:
        package, exported = imported
        return package, member if exported in {"*", "default"} and member else exported
    if callee == "fetch":
        return "global", "fetch"
    if ctx.framework == "nuxt" and callee in _NUXT_AUTO_IMPORTS:
        if callee in _VUE_REACTIVITY_EXPORTS:
            return "vue", callee
        return "nuxt", callee
    if (
        ctx.path.suffix == ".vue"
        and re.search(rb"<script\b[^>]*\bsetup\b", ctx.raw)
        and callee in {"defineProps", "defineEmits"}
    ):
        return "vue-macro", callee
    return None
