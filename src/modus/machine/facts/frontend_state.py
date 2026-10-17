"""Lexical state, effects and request observations for web source files.

Phases run in declaration/binding order so local shadows override imports and
reactive state. Request continuations and state reads share the same source-local
context; unresolved targets remain candidates rather than runtime facts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from tree_sitter import Node


from modus.machine.facts.frontend_context import (
    _add_node,
    _bound,
    _expression,
    _Extraction,
    _lexical_scope,
    _link,
    _names_in,
    _origin,
    _owner_at,
    _walk,
)

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# (package, export) pairs whose call results are tracked as reactive state.
_STATE_BINDINGS = {
    ("react", "useState"),
    ("nuxt", "useState"),
    ("react", "useReducer"),
    ("vue", "ref"),
    ("vue", "reactive"),
    ("vue", "computed"),
    ("pinia", "defineStore"),
    ("nuxt", "ref"),
    ("nuxt", "reactive"),
    ("nuxt", "computed"),
}

# (package, export) pairs recorded as effect or component-contract observations.
_EFFECT_BINDINGS = {
    ("vue-macro", "defineProps"),
    ("vue-macro", "defineEmits"),
    ("react", "useEffect"),
    ("react", "useLayoutEffect"),
    ("vue", "watch"),
    ("vue", "watchEffect"),
    ("nuxt", "useAsyncData"),
}

# (package, export) pairs of fetch-family request clients.
_FETCH_BINDINGS = {
    ("global", "fetch"),
    ("ofetch", "$fetch"),
    ("ofetch", "ofetch"),
    ("nuxt", "$fetch"),
    ("nuxt", "useFetch"),
    ("#app", "useFetch"),
}

_AXIOS_REQUEST_EXPORTS = {"get", "post", "put", "patch", "delete", "request", "default"}


def _annotate_existing_nodes(ctx: _Extraction) -> dict[str, list[dict]]:
    """Stamp runtime/framework facts onto parser rows and index them by bare label."""
    by_name: dict[str, list[dict]] = {}
    for row in ctx.nodes:
        label = str(row.get("label", "")).removeprefix(".").removesuffix("()")
        by_name.setdefault(label, []).append(row)
        row["runtimeBoundary"] = ctx.runtime
        if ctx.path.suffix == ".vue" and row["id"] == ctx.file_id:
            row["frontendRole"] = "component"
        if ctx.framework:
            row["framework"] = ctx.framework
    return by_name


def _collect_declarations(
    ctx: _Extraction, syntax_nodes: list[Node], by_name: dict[str, list[dict]]
) -> None:
    """Record function-like declaration spans and classify owners as component or handler.

    Fills ``ctx.declarations`` and mutates matched parser rows in place
    (``source_end_line``/``frontendRole``); owners are matched by nearest line.
    """
    for syntax in syntax_nodes:
        name_node = syntax.child_by_field_name("name")
        value = syntax.child_by_field_name("value")
        if syntax.type in {"function_declaration", "function_expression", "method_definition"} or (
            syntax.type == "variable_declarator"
            and value
            and value.type in {"arrow_function", "function_expression"}
        ):
            if name_node:
                name = ctx.raw[name_node.start_byte : name_node.end_byte].decode()
                matches = by_name.get(name, [])
                if matches:
                    owner = min(
                        matches,
                        key=lambda row: abs(
                            int(str(row.get("source_location", "L1")).lstrip("L").split(":")[0])
                            - syntax.start_point[0]
                            - 1
                        ),
                    )
                    owner.update(
                        source_end_line=syntax.end_point[0] + 1,
                        frontendRole="component"
                        if name[:1].isupper()
                        and any(child.type.startswith("jsx_") for child in _walk(syntax))
                        else "handler",
                    )
                    ctx.declarations.append((syntax.start_byte, syntax.end_byte, owner["id"]))


def _component_props_contracts(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Publish non-empty parameter lists of component-role functions as props contracts."""
    # Component inputs are declaration contracts; their runtime values remain unknown.
    for syntax in syntax_nodes:
        if syntax.type not in {"function_declaration", "arrow_function", "function_expression"}:
            continue
        parameters = syntax.child_by_field_name("parameters") or syntax.child_by_field_name(
            "parameter"
        )
        if (
            parameters
            and _expression(ctx.raw, parameters).strip("() ")
            and any(start <= syntax.start_byte <= end for start, end, _ in ctx.declarations)
        ):
            owner = _owner_at(ctx, syntax.start_byte)
            info: dict = next((row for row in ctx.nodes if row["id"] == owner), {})
            if info.get("frontendRole") == "component":
                props = _add_node(
                    ctx,
                    "component-contract",
                    "props",
                    parameters.start_byte,
                    parameters.end_byte,
                    contracts=[
                        {
                            "kind": "component-props",
                            "identity": _expression(ctx.raw, parameters),
                            "resolution": "declared",
                        }
                    ],
                )
                _link(ctx, owner, props, "accepts", parameters.start_byte)


def _collect_lexical_bindings(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Fill ``ctx.bindings`` with lexical scopes and ``ctx.handlers`` with function ids.

    Reads later resolve against these bindings, including non-state shadows.
    """
    for syntax in syntax_nodes:
        if syntax.type == "variable_declarator":
            for name in _names_in(ctx.raw, syntax.child_by_field_name("name")):
                ctx.bindings.setdefault(name, []).append(
                    (*_lexical_scope(ctx.raw, syntax), syntax.start_byte)
                )
                value = syntax.child_by_field_name("value")
                if value is not None and value.type in {"arrow_function", "function_expression"}:
                    match = next(
                        (
                            node_id
                            for start, _end, node_id in ctx.declarations
                            if start == syntax.start_byte
                        ),
                        None,
                    )
                    if match:
                        ctx.handlers[name, syntax.start_byte] = match
        if syntax.type in {
            "function_declaration",
            "arrow_function",
            "function_expression",
            "method_definition",
        }:
            parameters = syntax.child_by_field_name("parameters") or syntax.child_by_field_name(
                "parameter"
            )
            for name in _names_in(ctx.raw, parameters):
                ctx.bindings.setdefault(name, []).append(
                    (syntax.start_byte, syntax.end_byte, syntax.start_byte)
                )
            name_node = syntax.child_by_field_name("name")
            if name_node is not None:
                name = _expression(ctx.raw, name_node)
                ctx.bindings.setdefault(name, []).append(
                    (*_lexical_scope(ctx.raw, syntax), syntax.start_byte)
                )
                match = next(
                    (
                        node_id
                        for start, _end, node_id in ctx.declarations
                        if start == syntax.start_byte
                    ),
                    None,
                )
                if match is not None:
                    ctx.handlers[name, syntax.start_byte] = match


def _collect_state_declarations(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Add state nodes for declarators initialized by known reactive framework calls.

    Fills ``ctx.states``/``ctx.setters``/``ctx.state_offsets`` for later read/write edges.
    """
    # State initialization is observed at AST declarations, never from comments.
    for syntax in syntax_nodes:
        if syntax.type != "variable_declarator":
            continue
        name = syntax.child_by_field_name("name")
        value = syntax.child_by_field_name("value")
        # A declarator without a name pattern yields no bindable identifiers
        # below, so skipping it here is equivalent and keeps `name` narrowed.
        if name is None or value is None or value.type != "call_expression":
            continue
        callee = _expression(ctx.raw, value.child_by_field_name("function"))
        binding = _origin(ctx, callee, value.start_byte)
        if binding is None or binding not in _STATE_BINDINGS:
            continue
        names = re.findall(r"\b[A-Za-z_$][\w$]*\b", _expression(ctx.raw, name))
        if not names:
            continue
        state = _add_node(
            ctx,
            "state",
            names[0],
            syntax.start_byte,
            syntax.end_byte,
            frameworkExpression=callee,
            frameworkEvidence={
                "package": binding[0],
                "export": binding[1],
                "source": "import-or-framework-auto-import",
            },
            resolution="candidate",
        )
        ctx.states[names[0], syntax.start_byte] = state
        ctx.state_offsets.add((name.start_byte, name.end_byte))
        if len(names) > 1 and binding[1] in {"useState", "useReducer"}:
            ctx.setters[names[1], syntax.start_byte] = state
        _link(ctx, _owner_at(ctx, syntax.start_byte), state, "contains", syntax.start_byte)


def _record_effect(ctx: _Extraction, syntax: Node, callee: str, binding: tuple[str, str]) -> None:
    """Add an effect (or Vue macro component-contract) node contained by its owner."""
    kind = "component-contract" if binding[0] == "vue-macro" else "effect"
    node = _add_node(
        ctx,
        kind,
        callee,
        syntax.start_byte,
        syntax.end_byte,
        resolution="candidate",
        frameworkExpression=_expression(ctx.raw, syntax),
        frameworkEvidence={"package": binding[0], "export": binding[1]},
    )
    _link(ctx, _owner_at(ctx, syntax.start_byte), node, "contains", syntax.start_byte)


# 无法确定请求方法时保留 ANY，下游不得默认当作 GET。
def _request_method(raw: bytes, binding: tuple[str, str], callee: str, args: list[Node]) -> str:
    """Derive the HTTP method from the client callee and literal options; ANY when unsure."""
    method = callee.rsplit(".", 1)[-1].upper() if binding[0] == "axios" else "GET"
    method = method if method in _HTTP_METHODS else "ANY"
    # Only fetch-family options have this shape. axios.post's second
    # argument is request data, whose `method` field has no HTTP semantics.
    if binding and binding[0] != "axios" and len(args) > 1:
        options_node = args[1]
        method_nodes = [
            child.child_by_field_name("value")
            for child in options_node.named_children
            if child.type == "pair"
            and _expression(raw, child.child_by_field_name("key")).strip("\"'") == "method"
        ]
        if options_node.type != "object" or any(
            child.type == "spread_element" for child in options_node.named_children
        ):
            method = "ANY"
        elif method_nodes:
            raw_method = _expression(raw, method_nodes[0])
            method = (
                raw_method[1:-1].upper()
                if len(method_nodes) == 1
                and method_nodes[0] is not None
                and method_nodes[0].type == "string"
                else "ANY"
            )
            if method not in _HTTP_METHODS:
                method = "ANY"
    return method


# 响应延续关联请求所有者，但不把静态链式结构当作运行时完成证明。
def _link_response_continuations(ctx: _Extraction, syntax: Node, node: str) -> None:
    """Link awaits and then/catch/finally chains of a request back to its owner."""
    # Only syntactically present promise callbacks/await are linked. There is
    # no claim that the request executed or a server accepted it.
    parent = syntax.parent
    if parent and parent.type == "await_expression":
        _link(ctx, node, _owner_at(ctx, syntax.start_byte), "returns_to", syntax.start_byte)
    continuation = parent
    while continuation and continuation.type in {"member_expression", "call_expression"}:
        if continuation.type == "call_expression":
            function = continuation.child_by_field_name("function")
            prop = function.child_by_field_name("property") if function else None
            if _expression(ctx.raw, prop) in {"then", "catch", "finally"}:
                callback = _add_node(
                    ctx,
                    "response-handler",
                    _expression(ctx.raw, prop),
                    continuation.start_byte,
                    continuation.end_byte,
                    resolution="candidate",
                )
                _link(ctx, node, callback, "returns_to", continuation.start_byte)
                _link(
                    ctx,
                    callback,
                    _owner_at(ctx, syntax.start_byte),
                    "continues_in",
                    continuation.start_byte,
                    exact=False,
                )
        continuation = continuation.parent


def _record_request(
    ctx: _Extraction,
    syntax: Node,
    callee: str,
    binding: tuple[str, str],
    args: list[Node],
) -> None:
    """Add a candidate http request node with its consumed contract and continuations."""
    url = _expression(ctx.raw, args[0]) if args else ""
    value_summary = ctx.values.argument(
        line=syntax.start_point.row + 1,
        column=syntax.start_point.column + 1,
        operation=callee.rsplit(".", 1)[-1],
    )
    method = _request_method(ctx.raw, binding, callee, args)
    address = (
        value_summary.values[0] if value_summary.complete and len(value_summary.values) == 1 else ""
    )
    parsed_url = urlsplit(address)
    route = parsed_url.path if parsed_url.scheme in {"http", "https"} else address
    deployment_origin = (
        f"{parsed_url.scheme}://{parsed_url.netloc}"
        if parsed_url.scheme in {"http", "https"} and parsed_url.netloc
        else ""
    )
    contract = {
        "kind": "http",
        "direction": "consumes",
        "identity": f"{method} {route}" if route else url,
        "method": method,
        "path": route,
        "origin": deployment_origin,
        "valueSummary": value_summary.to_dict(),
        "client": callee,
        "clientBinding": {"package": binding[0], "export": binding[1]},
        "resolution": "candidate",
        "requiresVerification": (["deployment-origin"] if not deployment_origin else [])
        + ["route-mount"],
    }
    node = _add_node(
        ctx,
        "request",
        f"{method} {route or url}",
        syntax.start_byte,
        syntax.end_byte,
        contracts=[contract],
        resolution="candidate",
    )
    _link(ctx, _owner_at(ctx, syntax.start_byte), node, "invokes", syntax.start_byte)
    _link_response_continuations(ctx, syntax, node)


def _track_state_and_requests(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Single source-order pass linking state reads/writes, effects, and request calls.

    Kept as one traversal so node and edge emission order matches source order exactly.
    """
    # Hook/store/request invocations and assignments retain their actual owner.
    for syntax in syntax_nodes:
        if syntax.type == "identifier":
            state = _bound(ctx, ctx.states, _expression(ctx.raw, syntax), syntax.start_byte)
            if state and not any(
                start <= syntax.start_byte < end for start, end in ctx.state_offsets
            ):
                _link(
                    ctx,
                    _owner_at(ctx, syntax.start_byte),
                    state,
                    "reads",
                    syntax.start_byte,
                )
        if syntax.type in {
            "assignment_expression",
            "augmented_assignment_expression",
            "update_expression",
        }:
            left = _expression(
                ctx.raw,
                syntax.child_by_field_name("left") or syntax.child_by_field_name("argument"),
            )
            base = left.split(".")[0]
            state = _bound(ctx, ctx.states, base, syntax.start_byte)
            if state:
                _link(ctx, _owner_at(ctx, syntax.start_byte), state, "writes", syntax.start_byte)
        if syntax.type != "call_expression":
            continue
        callee = _expression(ctx.raw, syntax.child_by_field_name("function"))
        arguments = syntax.child_by_field_name("arguments")
        args = list(arguments.named_children) if arguments else []
        state = _bound(ctx, ctx.setters, callee, syntax.start_byte)
        if state:
            _link(ctx, _owner_at(ctx, syntax.start_byte), state, "writes", syntax.start_byte)
        binding = _origin(ctx, callee, syntax.start_byte)
        if binding in _EFFECT_BINDINGS:
            _record_effect(ctx, syntax, callee, binding)
        if binding is None:
            # An unbound callee can be neither a fetch-family nor an axios client.
            continue
        request_call = binding in _FETCH_BINDINGS or (
            binding[0] == "axios" and binding[1] in _AXIOS_REQUEST_EXPORTS
        )
        if not request_call:
            continue
        _record_request(ctx, syntax, callee, binding, args)
