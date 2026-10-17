"""Local framework declarations grounded in imports, lexical bindings and ASTs.

These adapters recognize explicit Python/Go route registrations. They neither
execute applications nor infer a deployment origin from a path or class name.
"""

from __future__ import annotations

import ast

from modus.machine.corpus.source_snapshot import SourcePath, parse_source
from modus.machine.facts.native_parser import file_identity
from modus.machine.facts.semantic_ir import stable_id
from modus.machine.facts.value_summary import SourceValueIndex


# 框架识别依赖词法绑定，不能仅凭函数或变量名称建立来源。
# 同名局部声明会遮蔽外层导入，歧义绑定保留为未知。
class FrameworkBindings:
    def __init__(self, facts: dict) -> None:
        self.scopes = {row["id"]: row for row in facts.get("scopes", ())}
        self.imports = facts.get("imports", ())
        self.root = next((row["id"] for row in self.scopes.values() if not row.get("parent")), "")
        self.references = {
            (row["line"], row["column"]): row
            for row in facts.get("references", ())
            if row.get("context") == "call"
        }
        self.routers: dict[tuple[str, str], dict] = {}

    # 调用位置关联已有引用事实；没有记录时回到文件根作用域。
    def scope_at(self, line: int, column: int) -> str:
        return str(self.references.get((line, column), {}).get("scope_id") or self.root)

    # 沿父作用域查找最近绑定，循环保护避免损坏作用域造成死循环。
    # CommonJS 的导入与 require 声明允许合并，其余多义绑定拒绝择一。
    def binding(self, scope_id: str, name: str) -> tuple[str, dict] | None:
        seen = set()
        while scope_id and scope_id not in seen:
            seen.add(scope_id)
            scope = self.scopes.get(scope_id, {})
            rows = [row for row in scope.get("bindings", ()) if row.get("name") == name]
            if rows:
                imported = [row for row in rows if row.get("kind") == "import"]
                ordinary = [row for row in rows if row.get("kind") != "import"]
                if (
                    len(imported) == 1
                    and len(ordinary) == 1
                    and str(ordinary[0].get("target_name") or "").startswith("require(")
                ):
                    return scope_id, imported[0]
                return (scope_id, rows[0]) if len(rows) == 1 else None
            scope_id = str(scope.get("parent") or "")
        return None

    # 别名展开设置深度上限，防止循环别名耗尽分析预算。
    # 只有唯一导入才能提供包名与导出符号的来源证据。
    def origin(self, scope: str, expression: str, depth: int = 0) -> tuple[str, str] | None:
        # 超出别名解析上限保持未知，递归深度不随项目输入无限增长。
        if depth > 8:
            return None
        root, _, member = expression.partition(".")
        located = self.binding(scope, root)
        if located is None:
            return None
        owner, binding = located
        if binding.get("kind") == "alias":
            alias = str(binding.get("target_name") or "")
            return self.origin(owner, alias + ("." + member if member else ""), depth + 1)
        if binding.get("kind") != "import":
            return None
        imported = [
            row for row in self.imports if row.get("scope_id") == owner and row.get("local") == root
        ]
        if len(imported) != 1:
            return None
        row = imported[0]
        exported = str(row.get("imported") or "*")
        return str(row["specifier"]), member if member and exported in {
            "*",
            "default",
        } else exported

    # 路由器实例按声明作用域和名称定位，隔离不同函数中的同名变量。
    def router(self, scope: str, name: str) -> dict | None:
        located = self.binding(scope, name)
        return self.routers.get((located[0], name)) if located else None

    # 处理器必须对应唯一声明，未解析名称不能生成精确调用目标。
    def handler(self, scope: str, name: str) -> str | None:
        located = self.binding(scope, name)
        return (
            str(located[1].get("target") or "")
            if located and located[1].get("kind") == "declaration"
            else None
        )


# 先收集挂载关系再解析，消除源码声明先后对前缀的影响。
# 循环、多重挂载与动态前缀保留未知状态，避免伪造应用级地址。
def resolve_router_mounts(
    routers: list[dict],
    mounts: dict[str, list[tuple[dict, str | None, bool]]],
) -> None:
    """Resolve explicit same-file mount dependencies without source-order bias.

    A repeated mount, cycle, dynamic prefix or chain beyond 32 is unknown. The
    local route remains available, but cannot masquerade as an application URL.
    """
    # 解析使用原始副本，避免已更新的路由器影响尚未解析的依赖。
    initial = {row["identity"]: dict(row) for row in routers}
    resolved: dict[str, dict] = {}

    def resolve(identity: str, ancestors: frozenset[str]) -> dict:
        # 共享父路由只计算一次，解析缓存复用于多个子路由。
        if identity in resolved:
            return resolved[identity]
        row = dict(initial[identity])
        if identity in ancestors or len(ancestors) >= 32:
            return {**row, "mounted": False, "prefixKnown": False}
        observations = mounts.get(identity, ())
        if len(observations) == 1:
            parent, prefix, replaces_local = observations[0]
            parent_row = resolve(parent["identity"], ancestors | {identity})
            if prefix is not None and parent_row.get("prefixKnown", True):
                row["prefix"] = (
                    str(parent_row.get("prefix") or "")
                    + prefix
                    + ("" if replaces_local else str(row.get("prefix") or ""))
                )
                row["mounted"] = bool(parent_row.get("mounted"))
            else:
                row.update(mounted=False, prefixKnown=False)
        elif observations:
            row.update(mounted=False, prefixKnown=False)
        resolved[identity] = row
        return row

    for router in routers:
        router.update(resolve(router["identity"], frozenset()))


# 本地路径与部署来源分别建模，路由注册不能证明线上地址。
# 处理器绑定可精确到声明，但不表示该处理器已被实际执行。
# 未找到唯一处理器时保留诊断，路由本身仍可作为候选证据。
def _publish_route(
    result: dict,
    relative: str,
    *,
    line: int,
    column: int,
    route: str,
    method: str,
    handler: str | None,
    package: str,
    router: dict,
) -> None:
    identity = stable_id("framework-entry", relative, line, column, method, route)
    # 应用相对路径要求挂载已知且前缀已知，两项证据缺一不可。
    mount_known = bool(router.get("mounted")) and router.get("prefixKnown", True)
    prefix = str(router.get("prefix") or "")
    full_path = "/" + (prefix.rstrip("/") + "/" + route.lstrip("/")).lstrip("/")
    contract = {
        "kind": "http",
        "direction": "provides",
        "identity": f"{method} {full_path}",
        "method": method,
        "path": full_path,
        "origin": "",
        "resolution": "candidate",
        "routerBinding": router["identity"],
        "pathScope": "application-relative" if mount_known else "router-local",
        "requiresVerification": ["deployment-origin"] + ([] if mount_known else ["route-mount"]),
    }
    result.setdefault("nodes", []).append(
        {
            "id": identity,
            "kind": "endpoint",
            "label": f"{method} {full_path}",
            "source_file": relative,
            "source_location": f"L{line}:{column}",
            "source_end_line": line,
            "contracts": [contract],
            "frameworkEvidence": {"package": package, "binding": "lexical-import"},
            "runtimeBoundary": "server",
            "analysis_completeness": "partial",
        }
    )
    # 有处理器身份才创建连接，避免图中出现无来源的隐式目标。
    if handler:
        result.setdefault("edges", []).append(
            {
                "source": identity,
                "target": handler,
                "relation": "handled_by",
                "resolution": "exact",
                "binding_kind": "declared-exact",
                "source_file": relative,
                "source_location": f"L{line}:{column}",
                "resolvedBy": "framework-registration",
                "evidence": [
                    "Explicit route registration names this handler; execution is not asserted"
                ],
            }
        )
    else:
        result.setdefault("diagnostics", []).append(
            {
                "code": "machine.facts.framework-handler-unresolved",
                "path": relative,
                "line": line,
                "message": "Route registration has no unique local handler binding",
            }
        )


# 只接受已知框架构造器及单变量赋值，限制路由器身份的推断范围。
# 动态前缀与空前缀不同，必须单独记录其已知状态。
def _python_routers(tree, bindings, relative) -> None:
    """Register constructors only when their lexical import binding is known."""
    constructors = {
        ("fastapi", "FastAPI"),
        ("fastapi", "APIRouter"),
        ("flask", "Flask"),
        ("flask", "Blueprint"),
    }
    for syntax in ast.walk(tree):
        if not isinstance(syntax, (ast.Assign, ast.AnnAssign)) or not isinstance(
            syntax.value, ast.Call
        ):
            continue
        targets = syntax.targets if isinstance(syntax, ast.Assign) else [syntax.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        call = syntax.value
        scope = bindings.scope_at(call.lineno, call.col_offset + 1)
        origin = bindings.origin(scope, ast.unparse(call.func))
        located = bindings.binding(scope, targets[0].id)
        if origin not in constructors or located is None:
            continue
        prefix = next(
            (
                kw.value.value
                for kw in call.keywords
                if kw.arg in {"prefix", "url_prefix"}
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ),
            "",
        )
        prefix_known = all(
            isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
            for kw in call.keywords
            if kw.arg in {"prefix", "url_prefix"}
        )
        bindings.routers[located[0], targets[0].id] = {
            "package": origin[0],
            "prefix": prefix,
            "prefixKnown": prefix_known,
            "mounted": origin[1] in {"FastAPI", "Flask"},
            "identity": stable_id("router-binding", relative, located[0], targets[0].id),
        }


# 挂载时区分追加前缀与 Flask 替换局部前缀的语义。
# 无法静态求值的前缀传递为未知，不能默认为空字符串。
def _python_mounts(tree, bindings) -> None:
    """Resolve mounts before publishing routes so prefixes are independent of source order."""
    # Explicit router inclusion carries its prefix. Ambiguous or repeated mounts
    # are retained as unknown instead of selecting a deployment by source order.
    mounts: dict[str, list[tuple[dict, str | None, bool]]] = {}
    for syntax in ast.walk(tree):
        if (
            not isinstance(syntax, ast.Call)
            or not isinstance(syntax.func, ast.Attribute)
            or syntax.func.attr not in {"include_router", "register_blueprint"}
        ):
            continue
        if (
            not isinstance(syntax.func.value, ast.Name)
            or not syntax.args
            or not isinstance(syntax.args[0], ast.Name)
        ):
            continue
        scope = bindings.scope_at(syntax.lineno, syntax.col_offset + 1)
        parent = bindings.router(scope, syntax.func.value.id)
        child = bindings.router(scope, syntax.args[0].id)
        if parent is None or child is None:
            continue
        prefix = next(
            (
                kw.value.value
                for kw in syntax.keywords
                if kw.arg in {"prefix", "url_prefix"}
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ),
            "",
        )
        dynamic_prefix = any(
            not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str)
            for kw in syntax.keywords
            if kw.arg in {"prefix", "url_prefix"}
        )
        replaces_local = syntax.func.attr == "register_blueprint" and any(
            kw.arg == "url_prefix" for kw in syntax.keywords
        )
        mounts.setdefault(child["identity"], []).append(
            (parent, None if dynamic_prefix else prefix, replaces_local)
        )
    resolve_router_mounts(list(bindings.routers.values()), mounts)


# 装饰器必须绑定到已识别路由器，名称相同的普通方法不触发识别。
# 仅发布完整求值的路由参数，避免把动态表达式当作字面路径。
# 通用 route 装饰器的 HTTP 方法展开有数量上限。
def _python_routes(tree, bindings, values, relative, result) -> None:
    """Publish decorator routes with their original declaration and source anchors."""
    declarations = {
        (row.get("name"), row.get("line")): row["id"] for row in result.get("declarations", ())
    }
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in function.decorator_list:
            if (
                not isinstance(decorator, ast.Call)
                or not isinstance(decorator.func, ast.Attribute)
                or not isinstance(decorator.func.value, ast.Name)
            ):
                continue
            method = decorator.func.attr.upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ROUTE"}:
                continue
            scope = bindings.scope_at(decorator.lineno, decorator.col_offset + 1)
            router = bindings.router(scope, decorator.func.value.id)
            if router is None:
                continue
            summary = values.argument(
                line=decorator.lineno,
                column=decorator.col_offset + 1,
                operation=decorator.func.attr,
            )
            # 静态值不完整时记录专门缺口，区别于文件没有路由声明。
            if not summary.complete:
                result.setdefault("diagnostics", []).append(
                    {
                        "code": "machine.facts.framework-route-value-unknown",
                        "path": relative,
                        "line": decorator.lineno,
                    }
                )
                continue
            methods = _python_route_methods(decorator, method)
            for route in summary.values:
                for verb in methods:
                    _publish_route(
                        result,
                        relative,
                        line=decorator.lineno,
                        column=decorator.col_offset + 1,
                        route=route,
                        method=verb,
                        handler=declarations.get((function.name, function.lineno)),
                        package=router["package"],
                        router=router,
                    )


def _python_route_methods(decorator: ast.Call, method: str) -> list[str]:
    """只有完整的字面值方法列表才能收窄通用 ROUTE 的范围。"""
    methods = [method]
    if method == "ROUTE":
        methods = ["ANY"]
        for keyword in decorator.keywords:
            if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                declared = [
                    item.value.upper()
                    for item in keyword.value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
                # 方法列表必须全部为字面值，不能只发布动态列表中可见的一部分。
                if len(declared) == len(keyword.value.elts) and len(declared) <= 8:
                    methods = declared
    return methods


# 同一源码依次建立路由器、挂载与入口，后续步骤使用前序证据。
# Python 语法不可解析时保留原有结构结果，不执行项目代码。
def _python(path: SourcePath, result: dict) -> dict:
    source = path.read_text()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return result
    relative = file_identity(path)[0]
    bindings = FrameworkBindings(result)
    values = SourceValueIndex(source, ".py")
    _python_routers(tree, bindings, relative)
    _python_mounts(tree, bindings)
    _python_routes(tree, bindings, values, relative, result)
    return result


# Go 构造器来源由导入绑定确认，支持集合明确列出。
# 路由器身份包含文件与作用域，避免跨文件同名对象碰撞。
def _go_routers(nodes, text, bindings, relative) -> None:
    """Register supported Go router constructors before resolving route calls."""
    constructors = {
        ("net/http", "NewServeMux"),
        ("github.com/gin-gonic/gin", "Default"),
        ("github.com/gin-gonic/gin", "New"),
        ("github.com/go-chi/chi/v5", "NewRouter"),
    }
    for syntax in nodes:
        if syntax.type not in {"short_var_declaration", "var_spec"}:
            continue
        left = syntax.child_by_field_name("left") or syntax.child_by_field_name("name")
        right = syntax.child_by_field_name("right") or syntax.child_by_field_name("value")
        if left is None or right is None:
            continue
        call = (
            right
            if right.type == "call_expression"
            else next((row for row in right.named_children if row.type == "call_expression"), None)
        )
        if call is None:
            continue
        scope = bindings.scope_at(call.start_point.row + 1, call.start_point.column + 1)
        origin = bindings.origin(scope, text(call.child_by_field_name("function")))
        name = text(left)
        located = bindings.binding(scope, name)
        if origin in constructors and located:
            bindings.routers[located[0], name] = {
                "package": origin[0],
                "prefix": "",
                "mounted": True,
                "identity": stable_id("router-binding", relative, located[0], name),
            }


# 可选解析依赖缺失时保留原结果，框架增强不影响基础事实可用性。
# 路由参数由静态值索引提供，注册目标仍需唯一声明绑定。
def _go(path: SourcePath, result: dict) -> dict:
    try:
        import tree_sitter_go
        from tree_sitter import Language, Parser
    except ImportError:
        return result
    raw = path.read_bytes()
    tree = parse_source(path, Parser(Language(tree_sitter_go.language())), raw)
    nodes = []
    pending = [tree.root_node]
    while pending:
        current = pending.pop()
        nodes.append(current)
        pending.extend(reversed(current.named_children))

    def text(node):
        return raw[node.start_byte : node.end_byte].decode() if node is not None else ""

    relative = file_identity(path)[0]
    bindings = FrameworkBindings(result)
    values = SourceValueIndex(raw.decode(), ".go")
    _go_routers(nodes, text, bindings, relative)
    for syntax in nodes:
        if syntax.type != "call_expression":
            continue
        callee = text(syntax.child_by_field_name("function"))
        receiver, separator, operation = callee.rpartition(".")
        if not separator:
            continue
        line, column = syntax.start_point.row + 1, syntax.start_point.column + 1
        scope = bindings.scope_at(line, column)
        imported = bindings.origin(scope, callee)
        router = bindings.router(scope, receiver)
        # 标准库默认 Mux 没有实例变量，需要独立的固定路由器身份。
        if imported == ("net/http", "HandleFunc"):
            router = {
                "package": "net/http",
                "prefix": "",
                "mounted": True,
                "identity": "net/http:default-mux",
            }
        if router is None or operation not in {
            "HandleFunc",
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "Get",
            "Post",
            "Put",
            "Patch",
            "Delete",
        }:
            continue
        summary = values.argument(line=line, column=column, operation=operation)
        arguments = syntax.child_by_field_name("arguments")
        args = list(arguments.named_children) if arguments else []
        handler = bindings.handler(scope, text(args[-1])) if len(args) >= 2 else None
        for route in summary.values if summary.complete else ():
            method = operation.upper() if operation != "HandleFunc" else "ANY"
            # 兼容标准库模式中的方法前缀，同时保留未知方法为 ANY。
            if operation == "HandleFunc" and " " in route:
                declared, route = route.split(" ", 1)
                method = (
                    declared
                    if declared in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                    else "ANY"
                )
            _publish_route(
                result,
                relative,
                line=line,
                column=column,
                route=route,
                method=method,
                handler=handler,
                package=router["package"],
                router=router,
            )
    return result


# 语言分派集中在入口，新适配器可独立扩展而不改变调用方协议。
def enrich_backend(path: SourcePath, result: dict) -> dict:
    if path.suffix == ".py":
        return _python(path, result)
    if path.suffix == ".go":
        return _go(path, result)
    return result
