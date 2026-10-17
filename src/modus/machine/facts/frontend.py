"""Framework-aware enrichment of parser facts for frontend and backend sources.

Web sources (React/Vue/Next/Nuxt components, Express/Nest registrations in
JS/TS) gain source-backed interaction observations without executing projects;
Python and Go sources are delegated to ``framework_facts.enrich_backend``.
Template and script positions always refer to the original file. Cross-service
requests remain candidate contracts until deployment and mount bindings are known.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from modus.machine.corpus.source_snapshot import SourcePath, parse_source
from modus.machine.facts.extraction_cache import project_root, resolver_cache
from modus.machine.facts.frontend_frameworks import (
    detect_framework as _detect_framework,
)
from modus.machine.facts.frontend_frameworks import (
    runtime_boundary as _runtime_boundary,
)
from modus.machine.facts.value_summary import SourceValueIndex
from modus.machine.store.ids import make_id

if TYPE_CHECKING:
    from tree_sitter import Node


from modus.machine.facts.frontend_context import (
    _add_node,
    _bound,
    _expression,
    _Extraction,
    _import_origins,
    _link,
    _owner_at,
    _walk,
)
from modus.machine.facts.frontend_server import (
    _filesystem_routes,
    _mount_express_routers,
    _publish_express_routes,
    _publish_nest_routes,
    _register_express_routers,
    _router_config_routes,
)
from modus.machine.facts.frontend_state import (
    _annotate_existing_nodes,
    _collect_declarations,
    _collect_lexical_bindings,
    _collect_state_declarations,
    _component_props_contracts,
    _track_state_and_requests,
)

_WEB_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro"}

# Upper bound for reading a previously cached enrichment result back from disk.
_CACHE_RESULT_MAX_BYTES = 64 * 1024 * 1024


_FRAMEWORK_INPUTS: ContextVar[dict[Path, bytes | None] | None] = ContextVar(
    "modus_framework_inputs", default=None
)


# 依赖清单的缺失状态也参与缓存身份，新建 package.json 会使旧结果失效。
def _manifest_inputs(path: SourcePath) -> dict[Path, bytes | None]:
    """Capture candidates, including absence, under the current source snapshot."""
    from modus.machine.corpus.input_snapshot import read_bytes

    root = project_root() or path.parent
    inputs: dict[Path, bytes | None] = {}
    memo = resolver_cache("framework-manifests")
    for parent in (path.parent, *path.parent.parents):
        if not parent.is_relative_to(root):
            break
        manifest = parent / "package.json"
        if manifest in memo:
            inputs[manifest] = memo[manifest]
            continue
        try:
            inputs[manifest] = read_bytes(root, manifest)
        except (OSError, ValueError):
            inputs[manifest] = None
        memo[manifest] = inputs[manifest]
    return inputs


# 缓存指纹覆盖协调入口、访问器实现和语法包版本，代码拆分不能漏掉依赖。
def _framework_implementation() -> str:
    """Digest of the extraction implementation and grammar versions for cache keys."""
    memo = resolver_cache("frontend-implementation")
    if "digest" not in memo:
        digest = hashlib.sha256()
        from modus.machine.corpus import source_snapshot as _source_snapshot
        from modus.machine.facts import (
            framework_facts as _framework_facts,
        )
        from modus.machine.facts import frontend_context, frontend_server, frontend_state
        from modus.machine.facts import (
            native_parser as _native_parser,
        )
        from modus.machine.facts import native_syntax as _native_syntax
        from modus.machine.facts import native_visitor as _native_visitor
        from modus.machine.facts import (
            value_summary as _value_summary,
        )

        for module_file in (
            __file__,
            frontend_context.__file__,
            frontend_state.__file__,
            frontend_server.__file__,
            _framework_facts.__file__,
            _value_summary.__file__,
            _native_parser.__file__,
            _native_syntax.__file__,
            _native_visitor.__file__,
            _source_snapshot.__file__,
        ):
            digest.update(Path(module_file).read_bytes())
        for package in (
            "tree-sitter",
            "tree-sitter-javascript",
            "tree-sitter-typescript",
            "tree-sitter-python",
            "tree-sitter-go",
        ):
            try:
                installed = version(package)
            except PackageNotFoundError:
                installed = "unavailable"
            digest.update((package + ":" + installed).encode())
        memo["digest"] = digest.hexdigest()
    return str(memo["digest"])


# 基础事实先深拷贝再增强，缓存投影不能反向修改源码局部提取结果。
# 缓存损坏或写入失败只影响复用，不撤销当前观察到的事实。
def enrich_frontend_cached(path: SourcePath, facts: dict) -> dict:
    """Rebuildable enrichment cache over immutable source-local parser facts.

    Parser facts, immutable source bytes, implementation and every potential
    manifest (including missing ones) determine the key. Configuration changes
    invalidate this projection without invalidating the source-local parser.
    """
    from modus.artifacts_io import atomic_write_text
    from modus.knowledge_reader import read_bound_regular_bytes
    from modus.paths import ModusPaths

    root = project_root() or path.parent
    inputs = _manifest_inputs(path) if path.suffix in _WEB_SUFFIXES else {}
    dependencies = {
        name.relative_to(root).as_posix(): hashlib.sha256(raw).hexdigest()
        if raw is not None
        else "missing-or-unavailable"
        for name, raw in inputs.items()
    }
    identity = {
        "source": hashlib.sha256(path.read_bytes()).hexdigest(),
        "path": path.relative_to(root).as_posix(),
        "facts": facts,
        "implementation": _framework_implementation(),
        "dependencies": dependencies,
    }

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    key = hashlib.sha256(canonical(identity).encode()).hexdigest()
    target = (
        ModusPaths.for_root(root).analysis_extraction_cache_dir
        / "framework-facts"
        / (key + ".json")
    )
    try:
        saved = json.loads(
            read_bound_regular_bytes(root, target, max_bytes=_CACHE_RESULT_MAX_BYTES)
        )
        if (
            isinstance(saved, dict)
            and saved.get("key") == key
            and isinstance(saved.get("result"), dict)
            # 命中缓存除键一致外还校验正文摘要，拒绝损坏或错配的结果。
            and saved.get("resultDigest")
            == hashlib.sha256(canonical(saved["result"]).encode()).hexdigest()
        ):
            return saved["result"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    # 上下文变量绑定本次清单快照，嵌套调用结束后必须恢复。
    token = _FRAMEWORK_INPUTS.set(inputs)
    try:
        result = enrich_frontend(path, deepcopy(facts))
    finally:
        _FRAMEWORK_INPUTS.reset(token)
    result["frameworkDependencies"] = dependencies
    try:
        payload = canonical(
            {
                "key": key,
                "result": result,
                "resultDigest": hashlib.sha256(canonical(result).encode()).hexdigest(),
            }
        )
        if len(payload.encode()) <= _CACHE_RESULT_MAX_BYTES:
            atomic_write_text(target, payload)
    except (OSError, ValueError):
        # A failed optional cache write cannot retract the observed facts.
        pass
    return result


# 事件属性绑定到可定位处理器后形成页面动作，名称本身不证明执行顺序。
def _jsx_page_actions(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Add page-action nodes for on* JSX attributes and invoke edges to bound handlers."""
    # JSX events are extracted from attributes, not arbitrary source strings.
    for syntax in syntax_nodes:
        if syntax.type != "jsx_attribute":
            continue
        value = _expression(ctx.raw, syntax)
        match = re.match(r"(on[A-Z]\w*)\s*=\s*\{(.*)\}", value, re.S)
        if match:
            event = _add_node(
                ctx,
                "page-action",
                match[1],
                syntax.start_byte,
                syntax.end_byte,
                contracts=[
                    {
                        "kind": "component-event",
                        "identity": match[1],
                        "handler": match[2],
                        "resolution": "declared",
                    }
                ],
            )
            _link(ctx, _owner_at(ctx, syntax.start_byte), event, "contains", syntax.start_byte)
            handler_id = _bound(ctx, ctx.handlers, match[2].strip(), syntax.start_byte)
            if handler_id:
                _link(ctx, event, handler_id, "invokes", syntax.start_byte)


# 状态读取只在 JSX 表达式内建立渲染关系，保持观察范围可解释。
def _jsx_state_renders(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Add renders edges from states read inside JSX expressions to their owner."""
    for syntax in syntax_nodes:
        if syntax.type == "jsx_expression":
            for child in _walk(syntax):
                if child.type != "identifier":
                    continue
                state = _bound(ctx, ctx.states, _expression(ctx.raw, child), syntax.start_byte)
                if state:
                    _link(
                        ctx,
                        state,
                        _owner_at(ctx, syntax.start_byte),
                        "renders",
                        syntax.start_byte,
                    )


# 模板事件、双向绑定和插值使用不同关系，避免把读状态与写状态混淆。
def _vue_template_interactions(ctx: _Extraction) -> None:
    """Extract Vue template events, v-model writes, and interpolation reads.

    Script/style/comment regions are masked with spaces so every match keeps
    its original byte offset in the file.
    """
    # Mask script/style/comments; event attributes retain original byte offsets.
    template = bytearray(ctx.raw)
    for pattern in (
        rb"<script\b[^>]*>.*?</script\s*>",
        rb"<style\b[^>]*>.*?</style\s*>",
        rb"<!--.*?-->",
    ):
        for template_match in re.finditer(pattern, ctx.raw, re.S | re.I):
            template[template_match.start() : template_match.end()] = b" " * (
                template_match.end() - template_match.start()
            )
    for template_match in re.finditer(
        rb"(?:@|v-on:)([\w:.-]+)\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]", bytes(template)
    ):
        event_name, handler = (part.decode() for part in template_match.groups())
        event = _add_node(
            ctx,
            "page-action",
            event_name,
            template_match.start(),
            template_match.end(),
            contracts=[
                {
                    "kind": "component-event",
                    "identity": event_name,
                    "handler": handler,
                    "resolution": "declared",
                }
            ],
        )
        _link(ctx, ctx.file_id, event, "contains", template_match.start())
        handler_name = re.match(r"^([A-Za-z_$][\w$]*)\s*(?:\(|$)", handler)
        handler_id = (
            _bound(ctx, ctx.handlers, handler_name[1], template_match.start())
            if handler_name
            else None
        )
        if handler_id:
            _link(ctx, event, handler_id, "invokes", template_match.start())
    for template_match in re.finditer(
        rb"v-model(?:\.[\w-]+)*\s*=\s*[\"\x27]([\w$]+)(?:\.value)?[\"\x27]", bytes(template)
    ):
        state = _bound(ctx, ctx.states, template_match[1].decode(), template_match.start())
        if state:
            action = _add_node(
                ctx, "page-action", "v-model", template_match.start(), template_match.end()
            )
            _link(ctx, action, state, "writes", template_match.start())
            _link(ctx, state, ctx.file_id, "renders", template_match.start())
    for template_match in re.finditer(rb"\{\{(.*?)\}\}", bytes(template), re.S):
        for name in re.findall(r"\b[A-Za-z_$][\w$]*\b", template_match[1].decode()):
            state = _bound(ctx, ctx.states, name, template_match.start())
            if state:
                _link(ctx, state, ctx.file_id, "renders", template_match.start())


# 按语言和已确认框架分派增强，基础解析结果始终作为输入事实保留。
def enrich_frontend(path: SourcePath, result: dict) -> dict:
    """Enrich parser facts with framework observations; delegates .py/.go to backend."""
    if path.suffix in {".py", ".go"}:
        from modus.machine.facts.framework_facts import enrich_backend

        return enrich_backend(path, result)
    if path.suffix not in _WEB_SUFFIXES:
        return result
    try:
        import tree_sitter_javascript
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
    except ImportError:
        result.setdefault("diagnostics", []).append(
            {"code": "machine.facts.frontend-grammar-unavailable", "file": path.name}
        )
        return result

    from modus.machine.facts.native_parser import component_script_bytes, file_identity

    manifest_inputs = _FRAMEWORK_INPUTS.get()
    if manifest_inputs is None:
        manifest_inputs = _manifest_inputs(path)
    framework, framework_root = _detect_framework(manifest_inputs)
    raw = path.read_bytes()
    masked, ts = (
        component_script_bytes(path, astro=path.suffix == ".astro")
        if path.suffix in {".vue", ".svelte", ".astro"}
        else (raw, path.suffix in {".ts", ".tsx"})
    )
    grammar = (
        tree_sitter_typescript.language_tsx()
        if path.suffix == ".tsx"
        else tree_sitter_typescript.language_typescript()
        if ts
        else tree_sitter_javascript.language()
    )
    tree = parse_source(path, Parser(Language(grammar)), masked)
    origins = _import_origins(tree.root_node, masked)
    values = SourceValueIndex(
        masked.decode("utf-8", "replace"),
        ".tsx" if path.suffix == ".tsx" else ".ts" if ts else ".js",
    )
    text = raw.decode("utf-8")
    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    prefix = file_identity(path)[2]
    file_id = next((row["id"] for row in nodes if row.get("kind") == "file"), make_id(str(path)))
    runtime = _runtime_boundary(path, text, framework, framework_root)
    syntax_nodes = list(_walk(tree.root_node))
    ctx = _Extraction(
        path=path,
        raw=raw,
        prefix=prefix,
        file_id=file_id,
        runtime=runtime,
        framework=framework,
        origins=origins,
        values=values,
        result=result,
        nodes=nodes,
        edges=edges,
    )
    return _enrich_web_observations(ctx, syntax_nodes, tree.root_node, framework_root)


# 各观察阶段按依赖顺序执行，路由器和绑定先于使用它们的关系。
def _enrich_web_observations(
    ctx: _Extraction, syntax_nodes: list[Node], root: Node, framework_root: Path | None
) -> dict:
    """Apply observation phases in their existing dependency order."""
    path, framework, result = ctx.path, ctx.framework, ctx.result
    from modus.machine.facts.native_parser import file_identity

    by_name = _annotate_existing_nodes(ctx)
    _collect_declarations(ctx, syntax_nodes, by_name)
    _component_props_contracts(ctx, syntax_nodes)
    # Resolve reads against lexical bindings, including non-state shadows.
    _collect_lexical_bindings(ctx, syntax_nodes)
    _collect_state_declarations(ctx, syntax_nodes)
    _track_state_and_requests(ctx, syntax_nodes)
    # Explicit Express and Nest registrations share the backend contract shape.
    from modus.machine.facts.framework_facts import FrameworkBindings

    server_bindings = FrameworkBindings(result)
    relative = file_identity(path)[0]
    _register_express_routers(ctx, syntax_nodes, server_bindings)
    _mount_express_routers(ctx, syntax_nodes, server_bindings)
    _publish_express_routes(ctx, syntax_nodes, server_bindings, relative)
    _publish_nest_routes(ctx, syntax_nodes, relative)
    _jsx_page_actions(ctx, syntax_nodes)
    _jsx_state_renders(ctx, syntax_nodes)
    if path.suffix == ".vue":
        _vue_template_interactions(ctx)
    _router_config_routes(ctx, syntax_nodes)
    # File routing requires observed framework dependencies. Config prefixes and
    # deployment mounts remain unresolved instead of manufacturing exact URLs.
    if framework_root is not None and framework in {"next", "nuxt"}:
        _filesystem_routes(ctx, server_bindings, relative, root, framework_root)
    result["analysisCompleteness"] = (
        "partial" if root.has_error else result.get("analysisCompleteness", "structural")
    )
    return result
