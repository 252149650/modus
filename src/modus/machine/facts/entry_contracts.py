"""Optional framework observations, selected by source syntax, never business owners."""

from __future__ import annotations

import ast
import re
from typing import Any


# 增强只修改复制后的节点，结构提取缓存保持独立。
# Python 使用标准 AST 读取装饰器，语法不完整时保留原结构观察。
# 函数名称与行号共同匹配，避免同名声明的契约互相污染。
# 只接纳显式字符串路径和方法列表，不求值装饰器参数。
# Go 接收者信息用于补充方法所有者，不推断动态接口分派。
# 模板事件保留 handler 原表达式，后续可按源码见证核查。
# 事件身份包含字符位置，同名事件在不同位置不会合并。
# 这些信号是声明观察，不能直接认定为业务模块所有权。
# 新增框架应在已理解语法范围内扩展，未知表达式保持未解析。
def augment_entries(
    relative: str,
    content: str,
    language: str,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    result = {**extraction, "nodes": [dict(row) for row in extraction.get("nodes", [])]}
    nodes = result["nodes"]
    if language == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return result
        for syntax in ast.walk(tree):
            if not isinstance(syntax, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            row = next(
                (
                    row
                    for row in nodes
                    if str(row.get("label", "")).removeprefix(".").removesuffix("()") == syntax.name
                    and str(row.get("source_location")) == f"L{syntax.lineno}"
                ),
                None,
            )
            if row is None:
                continue
            row["doc_comment"] = ast.get_docstring(syntax) or ""
            row["decorators"] = [
                ast.get_source_segment(content, value) or "" for value in syntax.decorator_list
            ]
            for decorator in syntax.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(
                    decorator.func, ast.Attribute
                ):
                    continue
                action = decorator.func.attr.lower()
                if action not in {
                    "get",
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "head",
                    "options",
                    "route",
                }:
                    continue
                if (
                    not decorator.args
                    or not isinstance(decorator.args[0], ast.Constant)
                    or not isinstance(decorator.args[0].value, str)
                ):
                    continue
                methods = [action.upper()] if action != "route" else ["ANY"]
                for keyword in decorator.keywords:
                    if keyword.arg == "methods" and isinstance(
                        keyword.value, (ast.List, ast.Tuple)
                    ):
                        methods = [
                            item.value.upper()
                            for item in keyword.value.elts
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        ]
                row["contracts"] = [
                    {
                        "kind": "http",
                        "identity": f"{method} {decorator.args[0].value}",
                        "direction": "provides",
                        "resolution": "declared",
                        "frameworkExpression": ast.get_source_segment(content, decorator),
                    }
                    for method in methods
                ]
    if language == "go":
        for row in nodes:
            line = int(str(row.get("source_location", "L1")).lstrip("L").split(":")[0])
            source = "\n".join(content.splitlines()[max(0, line - 1) : line + 2])
            match = re.match(
                r"\s*func\s*\(\s*\w+\s+\*?([\w.]+)(?:\[[^]]+\])?\s*\)\s*(\w+)\s*\(", source
            )
            if match and match[2] in str(row.get("label", "")):
                row["owner"], row["kind"] = match[1], "method"
    # Template event syntax is an explicit binding observation. The expression
    # is retained; dynamic handlers are not promoted to exact call edges.
    suffix = relative.rsplit(".", 1)[-1].lower()
    patterns = []
    if suffix == "vue":
        patterns = [r"(?:@|v-on:)([\w:.-]+)\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]"]
    elif suffix in {"svelte", "astro", "jsx", "tsx"}:
        patterns = [r"\b(on:[\w-]+|on[A-Z]\w*|onclick|onsubmit|onchange)\s*=\s*\{([^}]+)\}"]
    elif suffix in {"razor", "cshtml"}:
        patterns = [r"@(on\w+)\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]"]
    elif relative.endswith(".blade.php"):
        patterns = [r"wire:([\w.-]+)\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]"]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            line = content.count("\n", 0, match.start()) + 1
            nodes.append(
                {
                    "id": f"action:{relative}:{match.start()}",
                    "label": match[1],
                    "kind": "page-action",
                    "symbol": f"{relative}#event:{match[1]}:{line}",
                    "source_location": f"L{line}",
                    "signature": match[0],
                    "source_end_line": line + match[0].count("\n"),
                    "contracts": [
                        {
                            "kind": "component-event",
                            "identity": match[1],
                            "handler": match[2],
                            "resolution": "declared",
                        }
                    ],
                }
            )
    return result


# 先按种类与声明身份收集提供方，再匹配消费方。
# 同一契约可能有多个提供者，输出候选关系而非任选唯一目标。
# 源码字符串相等无法证明部署来源、客户端绑定或挂载路径相同。
# requiresVerification 显式保留这些运行时核查条件。
# 双方位置都进入关系，便于读者追溯声明比较的依据。
def link_declared_contracts(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join matching declarations in this page, not unverified runtime calls."""
    providers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        for contract in entry["contracts"]:
            if contract.get("direction") == "provides":
                providers.setdefault((contract["kind"], contract["identity"]), []).append(entry)
    links = []
    for entry in entries:
        for contract in entry["contracts"]:
            if contract.get("direction") != "consumes":
                continue
            for target in providers.get((contract["kind"], contract["identity"]), []):
                links.append(
                    {
                        "source": entry["entryId"],
                        "target": target["entryId"],
                        "relation": "matching-declared-contract",
                        "resolution": "candidate",
                        "contractKey": contract["identity"],
                        "bindingKind": "contract",
                        "location": entry["location"],
                        "targetLocation": target["location"],
                        "requiresVerification": [
                            "client-binding",
                            "deployment-origin",
                            "route-mount",
                        ],
                        "provenance": {"kind": "source-contract-comparison"},
                    }
                )
    return links
