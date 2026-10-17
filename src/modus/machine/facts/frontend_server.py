"""Explicit server registrations and framework-backed file routes.

Express mounts, Nest decorators and Next/Nuxt conventions emit the existing
backend contract shape. Dependencies and lexical origins establish authority;
filenames or familiar callee spelling alone cannot establish an exact route.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modus.machine.facts.frontend_routes import (
    filesystem_route_projection as _filesystem_route_projection,
)
from modus.machine.store.ids import make_id

if TYPE_CHECKING:
    from tree_sitter import Node

    from modus.machine.facts.framework_facts import FrameworkBindings

from modus.machine.facts.frontend_context import (
    _add_node,
    _expression,
    _Extraction,
    _link,
    _origin,
)
from modus.machine.facts.frontend_state import (
    _HTTP_METHODS,
)

_EXPRESS_ROUTE_OPERATIONS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}

_NEST_ROUTE_DECORATORS = {"Get", "Post", "Put", "Patch", "Delete", "Head", "Options", "All"}

_ROUTER_FACTORY_BINDINGS = {
    ("react-router-dom", "createBrowserRouter"),
    ("react-router-dom", "createHashRouter"),
    ("vue-router", "createRouter"),
}

_EVENT_HANDLER_BINDINGS = {
    ("nuxt", "defineEventHandler"),
    ("h3", "defineEventHandler"),
}


# 只认领来源可证实的 Express 构造器，避免普通同名函数误匹配。
# 应用实例可视为已挂载，独立 Router 仍需后续挂载证据。
# 实例身份包含源码位置，隔离同名声明。
def _register_express_routers(
    ctx: _Extraction, syntax_nodes: list[Node], server_bindings: FrameworkBindings
) -> None:
    """Register variables initialized from express()/express.Router() as router bindings."""
    # A router instance belongs to a lexical binding, never to a variable name
    # seen elsewhere in the file.
    for syntax in syntax_nodes:
        if syntax.type != "variable_declarator":
            continue
        name = syntax.child_by_field_name("name")
        value = syntax.child_by_field_name("value")
        if (
            name is None
            or name.type != "identifier"
            or value is None
            or value.type != "call_expression"
        ):
            continue
        scope = server_bindings.scope_at(value.start_point.row + 1, value.start_point.column + 1)
        api = server_bindings.origin(
            scope, _expression(ctx.raw, value.child_by_field_name("function"))
        )
        located = server_bindings.binding(scope, _expression(ctx.raw, name))
        if (
            api not in {("express", "default"), ("express", "*"), ("express", "Router")}
            or located is None
        ):
            continue
        server_bindings.routers[located[0], _expression(ctx.raw, name)] = {
            "package": "express",
            "prefix": "",
            "mounted": api[1] != "Router",
            "identity": make_id(
                ctx.prefix, "router", _expression(ctx.raw, name), str(syntax.start_byte)
            ),
        }


# 先收集父子挂载再统一解析，避免源代码顺序改变结果。
# 单参数 use 表示空前缀；多参数调用需唯一静态前缀。
# 无法确定的前缀保留为未知，交由挂载解析器传播。
def _mount_express_routers(
    ctx: _Extraction, syntax_nodes: list[Node], server_bindings: FrameworkBindings
) -> None:
    """Collect app.use(...) parent/child router mounts and resolve mount prefixes."""
    from modus.machine.facts.framework_facts import resolve_router_mounts

    mounts: dict[str, list[tuple[dict, str | None, bool]]] = {}
    for syntax in syntax_nodes:
        if syntax.type != "call_expression":
            continue
        callee = _expression(ctx.raw, syntax.child_by_field_name("function"))
        receiver, _, operation = callee.rpartition(".")
        if operation != "use":
            continue
        arguments = syntax.child_by_field_name("arguments")
        args = list(arguments.named_children) if arguments else []
        scope = server_bindings.scope_at(syntax.start_point.row + 1, syntax.start_point.column + 1)
        parent = server_bindings.router(scope, receiver)
        child = server_bindings.router(scope, _expression(ctx.raw, args[-1])) if args else None
        if parent is None or child is None:
            continue
        summary = ctx.values.argument(
            line=syntax.start_point.row + 1,
            column=syntax.start_point.column + 1,
            operation=operation,
        )
        mount = (
            ""
            if len(args) == 1
            else summary.values[0]
            if summary.complete and len(summary.values) == 1
            else None
        )
        mounts.setdefault(child["identity"], []).append((parent, mount, False))
    resolve_router_mounts(list(server_bindings.routers.values()), mounts)


# HTTP 操作必须绑定到已登记路由器，方法名称本身不构成证据。
# 命名处理器优先使用词法声明，内联处理器按精确源码坐标绑定。
# 路由参数不完整时跳过发布，避免截取部分值造成假地址。
def _publish_express_routes(
    ctx: _Extraction,
    syntax_nodes: list[Node],
    server_bindings: FrameworkBindings,
    relative: str,
) -> None:
    """Publish router.<verb>(route, handler) registrations as backend route contracts."""
    from modus.machine.facts.framework_facts import _publish_route

    for syntax in syntax_nodes:
        if syntax.type != "call_expression":
            continue
        callee = _expression(ctx.raw, syntax.child_by_field_name("function"))
        receiver, _, operation = callee.rpartition(".")
        if operation not in _EXPRESS_ROUTE_OPERATIONS:
            continue
        line, column = syntax.start_point.row + 1, syntax.start_point.column + 1
        scope = server_bindings.scope_at(line, column)
        router = server_bindings.router(scope, receiver)
        if router is None:
            continue
        summary = ctx.values.argument(line=line, column=column, operation=operation)
        arguments = syntax.child_by_field_name("arguments")
        args = list(arguments.named_children) if arguments else []
        handler_id = (
            server_bindings.handler(scope, _expression(ctx.raw, args[-1]))
            if len(args) > 1
            else None
        )
        # 匿名处理器没有可解析名称，使用已有声明的精确行列位置补足。
        if handler_id is None and len(args) > 1:
            inline = args[-1]
            exact = [
                row["id"]
                for row in ctx.result.get("declarations", ())
                if row.get("line") == inline.start_point.row + 1
                and row.get("column") == inline.start_point.column + 1
            ]
            handler_id = exact[0] if len(exact) == 1 else None
        for route in summary.values if summary.complete else ():
            _publish_route(
                ctx.result,
                relative,
                line=line,
                column=column,
                route=route,
                method=operation.upper() if operation != "all" else "ANY",
                handler=handler_id,
                package="express",
                router=router,
            )


# Controller 装饰器必须来自 Nest 导入绑定，不能凭拼写判断框架。
# 空参数代表空前缀，动态参数代表未知前缀，两者不可混淆。
def _publish_nest_routes(ctx: _Extraction, syntax_nodes: list[Node], relative: str) -> None:
    """Publish @Controller classes with @Get/@Post/... methods as backend route contracts."""
    for controller in syntax_nodes:
        if controller.type != "class_declaration":
            continue
        decorator = next(
            (child for child in controller.named_children if child.type == "decorator"), None
        )
        call = decorator.named_children[0] if decorator and decorator.named_children else None
        if (
            call is None
            or call.type != "call_expression"
            or _origin(
                ctx, _expression(ctx.raw, call.child_by_field_name("function")), call.start_byte
            )
            != ("@nestjs/common", "Controller")
        ):
            continue
        arguments = call.child_by_field_name("arguments")
        summary = ctx.values.argument(
            line=call.start_point.row + 1,
            column=call.start_point.column + 1,
            operation=_expression(ctx.raw, call.child_by_field_name("function")),
        )
        base = (
            ""
            if arguments and not arguments.named_children
            else summary.values[0]
            if summary.complete and len(summary.values) == 1
            else None
        )
        _publish_nest_controller_methods(ctx, controller, relative, base)


# 控制器前缀与方法路径分开观察，最后按框架注册契约组合。
# 装饰器只关联紧随的受支持方法，遇到其他节点清除暂存状态。
# 模块挂载尚未观察，因此控制器路由仍保留未挂载状态。
def _publish_nest_controller_methods(
    ctx: _Extraction, controller: Node, relative: str, base: str | None
) -> None:
    """Bind method decorators after the controller prefix has been observed."""
    from modus.machine.facts.framework_facts import _publish_route

    body = controller.child_by_field_name("body")
    previous_decorator = None
    for child in body.named_children if body else ():
        if child.type == "decorator":
            previous_decorator = child
            continue
        if child.type != "method_definition" or previous_decorator is None:
            previous_decorator = None
            continue
        route_call = (
            previous_decorator.named_children[0] if previous_decorator.named_children else None
        )
        # 消费后立即清除装饰器，防止泄漏到后面的无关方法。
        previous_decorator = None
        if route_call is None or route_call.type != "call_expression":
            continue
        api = _origin(
            ctx,
            _expression(ctx.raw, route_call.child_by_field_name("function")),
            route_call.start_byte,
        )
        if api is None or api[0] != "@nestjs/common" or api[1] not in _NEST_ROUTE_DECORATORS:
            continue
        route_values = ctx.values.argument(
            line=route_call.start_point.row + 1,
            column=route_call.start_point.column + 1,
            operation=_expression(ctx.raw, route_call.child_by_field_name("function")),
        )
        route_args = route_call.child_by_field_name("arguments")
        local_routes = (
            ("",)
            if route_args and not route_args.named_children
            else route_values.values
            if route_values.complete
            else ()
        )
        handler_id = next(
            (node_id for start, _end, node_id in ctx.declarations if start == child.start_byte),
            None,
        )
        router = {
            "identity": make_id(ctx.prefix, "nest-controller", str(controller.start_byte)),
            "prefix": base or "",
            "mounted": False,
            "prefixKnown": base is not None,
        }
        for route in local_routes:
            _publish_route(
                ctx.result,
                relative,
                line=route_call.start_point.row + 1,
                column=route_call.start_point.column + 1,
                route=route,
                method=api[1].upper() if api[1] != "All" else "ANY",
                handler=handler_id,
                package="@nestjs/common",
                router=router,
            )


# 路径字面量必须位于已知路由工厂调用内，普通对象字段不视为页面。
# 配置声明产生候选页面边，挂载语义仍需后续验证。
# 沿语法父链寻找工厂调用，普通配置文件中的 path 字段不会单独成为路由。
def _router_config_routes(ctx: _Extraction, syntax_nodes: list[Node]) -> None:
    """Add page-route nodes for path literals inside React/Vue Router factory calls."""
    # Route objects (React Router/Vue Router); the mount still needs verification.
    for syntax in syntax_nodes:
        if syntax.type != "pair":
            continue
        key, value = syntax.child_by_field_name("key"), syntax.child_by_field_name("value")
        if (
            _expression(ctx.raw, key).strip('"\x27') != "path"
            or value is None
            or value.type != "string"
        ):
            continue
        container = syntax.parent
        routed = False
        while container is not None:
            if container.type == "call_expression":
                route_binding = _origin(
                    ctx,
                    _expression(ctx.raw, container.child_by_field_name("function")),
                    container.start_byte,
                )
                if route_binding in _ROUTER_FACTORY_BINDINGS:
                    routed = True
                    break
            container = container.parent
        if not routed:
            continue
        route = _expression(ctx.raw, value)[1:-1]
        node = _add_node(
            ctx,
            "page-route",
            route,
            syntax.start_byte,
            syntax.end_byte,
            contracts=[{"kind": "page-route", "identity": route, "resolution": "candidate"}],
        )
        _link(ctx, ctx.file_id, node, "exposes", syntax.start_byte, exact=False)


# 文件路由按框架约定区分具名 HTTP 导出与默认导出。
# 只有能绑定到本地声明的导出才成为实现目标。
# 返回实现 ID 供上层建立页面与处理器关系。
# 页面默认导出可关联实现，但不因此发布服务端 HTTP 契约。
def _export_route_handlers(
    ctx: _Extraction,
    server_bindings: FrameworkBindings,
    relative: str,
    route: str,
    route_method: str,
    role: str,
    routing_root: str,
) -> list[str]:
    """Publish routes for exported handlers of a filesystem route; return handler ids."""
    from modus.machine.facts.framework_facts import _publish_route

    implementation_ids: list[str] = []
    for exported in ctx.result.get("exports", ()):
        exported_name = str(exported.get("name") or "")
        handler_id = server_bindings.handler(
            str(exported.get("scope_id") or server_bindings.root),
            str(exported.get("local") or ""),
        )
        if handler_id is None:
            continue
        # Next 的 app 路由使用 HTTP 方法导出，与 pages 默认导出保持区分。
        named_http_export = (
            ctx.framework == "next" and routing_root == "app" and exported_name in _HTTP_METHODS
        )
        default_handler_export = exported_name == "default" and (
            (ctx.framework == "next" and routing_root == "pages") or ctx.framework == "nuxt"
        )
        if role == "server-handler" and (named_http_export or default_handler_export):
            verb = exported_name if exported_name != "default" else route_method
            _publish_route(
                ctx.result,
                relative,
                line=1,
                column=1,
                route=route,
                method=verb,
                handler=handler_id,
                package=ctx.framework,
                router={
                    "identity": make_id(ctx.prefix, "filesystem-router"),
                    "mounted": False,
                    "prefix": "",
                },
            )
            implementation_ids.append(handler_id)
        elif role == "page" and exported_name == "default":
            implementation_ids.append(handler_id)
    return implementation_ids


# 默认导出包装调用需来自 Nuxt 或 h3 的受支持 API。
# 回调按源码坐标唯一匹配，避免把包装器本身当成处理函数。
# 该回退仅补充未被普通导出解析覆盖的处理器。
def _nuxt_event_handler_fallback(
    ctx: _Extraction, root_node: Node, relative: str, route: str, route_method: str
) -> list[str]:
    """Resolve `export default defineEventHandler(cb)` callbacks; return matched handler ids."""
    from modus.machine.facts.framework_facts import _publish_route

    implementation_ids: list[str] = []
    for declaration in root_node.named_children:
        if declaration.type != "export_statement" or not _expression(
            ctx.raw, declaration
        ).lstrip().startswith("export default"):
            continue
        wrapper = declaration.child_by_field_name("value")
        if wrapper is None or wrapper.type != "call_expression":
            continue
        callee_node = wrapper.child_by_field_name("function")
        if (
            _origin(ctx, _expression(ctx.raw, callee_node), wrapper.start_byte)
            not in _EVENT_HANDLER_BINDINGS
        ):
            continue
        arguments_node = wrapper.child_by_field_name("arguments")
        callback = (
            arguments_node.named_children[0]
            if arguments_node and arguments_node.named_children
            else None
        )
        if callback is None:
            continue
        matched = [
            item["id"]
            for item in ctx.result.get("declarations", ())
            if item.get("line") == callback.start_point.row + 1
            and item.get("column") == callback.start_point.column + 1
        ]
        if len(matched) == 1:
            _publish_route(
                ctx.result,
                relative,
                line=callback.start_point.row + 1,
                column=callback.start_point.column + 1,
                route=route,
                method=route_method,
                handler=matched[0],
                package=ctx.framework,
                router={
                    "identity": make_id(ctx.prefix, "filesystem-router"),
                    "mounted": False,
                    "prefix": "",
                },
            )
            implementation_ids.append(matched[0])
    return implementation_ids


# 文件布局只产生候选路由，前缀、中间件和部署来源仍需验证。
# 处理器缺失时保留文件级回退关系，使路由证据仍可定位。
# 实现 ID 去重排序，保证重复导出不会产生重复连接。
def _filesystem_routes(
    ctx: _Extraction,
    server_bindings: FrameworkBindings,
    relative: str,
    root_node: Node,
    framework_root: Path,
) -> None:
    """Emit a candidate page-route node from file conventions and bind its handlers."""
    projection = _filesystem_route_projection(ctx.framework, ctx.path, framework_root)
    if projection is None:
        return
    route, route_method, role, routing_root = projection
    node = _add_node(
        ctx,
        "page-route",
        route,
        0,
        min(len(ctx.raw), 1),
        frameworkExpression="filesystem-route",
        framework=ctx.framework,
        routeRole=role,
        resolution="candidate",
        contracts=[
            {
                "kind": "page-route",
                "identity": route,
                "method": route_method,
                "resolution": "candidate",
                "requiresVerification": ["route-prefix", "middleware", "deployment-origin"],
            }
        ],
    )
    implementation_ids = _export_route_handlers(
        ctx, server_bindings, relative, route, route_method, role, routing_root
    )
    # 只在常规导出没有结果时启用框架特定回退，避免重复发布。
    if ctx.framework == "nuxt" and role == "server-handler" and not implementation_ids:
        implementation_ids.extend(
            _nuxt_event_handler_fallback(ctx, root_node, relative, route, route_method)
        )
    for handler_id in sorted(set(implementation_ids)):
        _link(ctx, node, handler_id, "handled_by", 0, exact=False)
    if not implementation_ids:
        # 文件级关系显式保持非精确，不把目录约定升级成函数绑定。
        _link(ctx, node, ctx.file_id, "renders" if role == "page" else "handled_by", 0, exact=False)
