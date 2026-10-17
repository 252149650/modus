"""Bounded source values from call arguments; never execute repository code.

This is a small abstract domain, not a data-flow solver. Literals, immutable
aliases, string concatenation and exhaustive conditional expressions yield at
most eight strings. Unsupported syntax, mutation, missing grammar or ambiguous
call locations yield unknown, never a guessed resource name.
"""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_VALUES = 8
MAX_VALUE_LENGTH = 200
MAX_EXPRESSION_DEPTH = 16
MAX_SYNTAX_NODES = 200_000


# 环境读取只保存变量名称及访问器，不读取当前机器上的实际值。
# 符号来源可用于配置关联，不等同于已知资源字面量。
# 符号引用与已知字面值分别建模，未知值仍可保留其词法来源。
@dataclass(frozen=True)
class SymbolReference:
    name: str
    accessor: str
    kind: str = "environment"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "accessor": self.accessor}


# 已知字符串、完整性、原因与符号依赖分别表达。
# 未知结果也有原因，调用方不能把空集合解释为资源不存在。
# 完整性标记决定调用方能否展开值集合，部分值不能冒充全部可能值。
@dataclass(frozen=True)
class ValueSummary:
    values: tuple[str, ...] = ()
    complete: bool = False
    reason: str = "unknown"
    symbols: tuple[SymbolReference, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": list(self.values),
            "complete": self.complete,
            "reason": self.reason,
            "symbols": [item.to_dict() for item in self.symbols],
        }


# 动态表达式保留可识别的符号依赖，不能拼接猜测值。
# 符号数量同样受限，超限时明确记录原因。
# 无法求得字面量时保留符号摘要，后续消费者可选择继续验证。
def _symbolic(reason: str, *summaries: ValueSummary) -> ValueSummary:
    symbols = sorted(
        {symbol for summary in summaries for symbol in summary.symbols},
        key=lambda item: (item.kind, item.name, item.accessor),
    )
    return ValueSummary(
        reason=reason if len(symbols) <= MAX_VALUES else "symbol-limit",
        symbols=tuple(symbols[:MAX_VALUES]),
    )


# 有限抽象值域只接纳短且非空字符串，数量超限即降为未知。
# 疑似敏感资源名拒绝公开，避免配置值通过证据摘要泄露。
# 排序去重使条件分支枚举顺序不影响摘要。
def _known(values: Any, reason: str) -> ValueSummary:
    selected: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE_LENGTH:
            return ValueSummary(reason="unsupported-value")
        # Infrastructure identifiers must not accidentally publish credentials.
        if any(part in value.lower() for part in ("password", "passwd", "secret", "token")):
            return ValueSummary(reason="sensitive-value")
        selected.add(value)
        if len(selected) > MAX_VALUES:
            return ValueSummary(reason="value-limit")
    return ValueSummary(tuple(sorted(selected)), bool(selected), reason)


_GRAMMARS = {
    ".py": ("python", "language"),
    ".java": ("java", "language"),
    ".js": ("javascript", "language"),
    ".jsx": ("javascript", "language"),
    ".mjs": ("javascript", "language"),
    ".cjs": ("javascript", "language"),
    ".ts": ("typescript", "language_typescript"),
    ".tsx": ("typescript", "language_tsx"),
    ".go": ("go", "language"),
    ".c": ("c", "language"),
    ".cpp": ("cpp", "language"),
    ".cc": ("cpp", "language"),
    ".h": ("cpp", "language"),
}
_SCOPES = frozenset(
    {
        "module",
        "program",
        "translation_unit",
        "source_file",
        "class_body",
        "class_definition",
        "function_definition",
        "function_declaration",
        "method_declaration",
        "constructor_declaration",
        "function_expression",
        "arrow_function",
        "lambda",
        "block",
        "statement_block",
    }
)
_CALLS = frozenset({"call", "call_expression", "method_invocation", "invocation_expression"})
_IDENTIFIERS = frozenset({"identifier", "type_identifier", "field_identifier"})


# 索引只负责单文件有界语法解释，不执行仓库代码。
# 调用位置、声明和遮蔽分别记录，定位不唯一时返回未知。
# 语法节点预算限制大型表达式占用，索引生命周期由上层控制。
class SourceValueIndex:
    """A file-local syntax index. The owner controls its lifetime and memory."""

    def __init__(self, source: str, suffix: str) -> None:
        self.source = source.encode("utf-8")
        self.suffix = suffix
        self.root: Any = None
        self.reason = "grammar-unavailable"
        self.calls: list[Any] = []
        self.call_positions: dict[tuple[str, int, int], Any] = {}
        self.call_lines: dict[tuple[str, int], list[Any]] = {}
        self.declarations: dict[tuple[int, str], list[Any]] = {}
        self.blocked: set[tuple[int, str]] = set()
        self.import_origins: dict[tuple[int, str], str] = {}
        grammar = _GRAMMARS.get(suffix)
        if grammar is None:
            return
        try:
            from tree_sitter import Language, Parser

            package = importlib.import_module("tree_sitter_" + grammar[0])
            self.root = (
                Parser(Language(getattr(package, grammar[1])())).parse(self.source).root_node
            )
        except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
            return
        self.reason = "unsupported-expression"
        stack = [self.root]
        visited = 0
        while stack:
            node = stack.pop()
            visited += 1
            if visited > MAX_SYNTAX_NODES:
                self.root = None
                self.calls.clear()
                self.declarations.clear()
                self.reason = "syntax-limit"
                return
            stack.extend(reversed(node.named_children))
            if node.type in _CALLS:
                self.calls.append(node)
                operation = self._call_name(node)
                self.call_positions[
                    (operation, node.start_point.row + 1, node.start_point.column + 1)
                ] = node
                # Huge multi-line expressions remain unknown away from their
                # start, rather than allocating an unbounded interval index.
                for row in range(
                    node.start_point.row, min(node.end_point.row + 1, node.start_point.row + 256)
                ):
                    self.call_lines.setdefault((operation, row + 1), []).append(node)
            self._observe_binding(node)
            self._observe_import_origin(node)

    def _observe_import_origin(self, node: Any) -> None:
        """记录受支持环境访问器的来源与遮蔽，不读取进程环境。"""
        if node.type == "import_statement" and self.suffix == ".py":
            raw = self._text(node)
            for module, alias in re.findall(r"(?:import\s+|,\s*)(os)(?:\s+as\s+(\w+))?", raw):
                self.import_origins[self._key(node, alias or module)] = module
        elif node.type == "import_from_statement" and self.suffix == ".py":
            raw = self._text(node)
            match = re.fullmatch(r"from\s+os\s+import\s+getenv(?:\s+as\s+(\w+))?", raw)
            if match:
                self.import_origins[self._key(node, match[1] or "getenv")] = "os.getenv"
        elif node.type == "import_spec" and self.suffix == ".go":
            raw = self._text(node)
            match = re.fullmatch(r'(?:([\w]+)\s+)?"os"', raw)
            if match:
                self.import_origins[self._key(node, match[1] or "os")] = "os"
        elif node.type == "import_statement" and self.suffix in {".js", ".ts", ".tsx", ".jsx"}:
            raw = self._text(node)
            if re.search(r"\bprocess\b", raw.partition("from")[0]):
                self.blocked.add(self._key(node, "process"))

    def _text(self, node: Any) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    # 值解析沿语法作用域向外查找，不跨过词法边界凭名称猜测。
    def _scope(self, node: Any) -> Any:
        current = node.parent
        while current is not None and current.type not in _SCOPES:
            current = current.parent
        return current if current is not None else self.root

    def _key(self, node: Any, name: str) -> tuple[int, str]:
        return (self._scope(node).id, name)

    # 仅可证明的不可变绑定可参与值传播，参数和变更操作会阻断。
    # 多个声明不任意取一项，后续 lookup 会保留歧义。
    # 导入或局部声明遮蔽环境访问器时，不再按系统环境读取解释。

    def _observe_binding(self, node: Any) -> None:
        kind = node.type
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        immutable = False
        if kind == "variable_declarator" and name is not None:
            parent_text = self._text(node.parent)
            immutable = parent_text.lstrip().startswith("const ") or "final " in parent_text
        elif kind == "const_spec":
            immutable = True
            if (
                value is not None
                and value.type == "expression_list"
                and len(value.named_children) == 1
            ):
                value = value.named_children[0]
        elif kind == "assignment":
            name = node.child_by_field_name("left")
            value = node.child_by_field_name("right")
            immutable = name is not None and self._text(name).isupper()
        else:
            self._observe_shadowing(node, kind, name)
            return
        if name is None or name.type not in _IDENTIFIERS:
            return
        key = self._key(node, self._text(name))
        if not immutable or value is None:
            self.blocked.add(key)
        else:
            self.declarations.setdefault(key, []).append(value)

    def _observe_shadowing(self, node: Any, kind: str, name: Any) -> None:
        """参数、声明与可变赋值阻止同名常量回溯，避免误用外层值。"""
        if kind in {
            "formal_parameter",
            "required_parameter",
            "optional_parameter",
            "parameter_declaration",
        }:
            if name is not None:
                self.blocked.add(self._key(node, self._text(name)))
            return
        elif kind in {"parameters", "formal_parameters", "lambda_parameters"}:
            for child in node.named_children:
                parameter = (
                    child if child.type == "identifier" else child.child_by_field_name("name")
                )
                if parameter is not None:
                    self.blocked.add(self._key(node, self._text(parameter)))
            return
        elif kind in {
            "function_declaration",
            "class_declaration",
            "function_definition",
            "class_definition",
        }:
            if name is not None:
                self.blocked.add(self._key(node, self._text(name)))
            return
        elif kind == "arrow_function":
            parameter = node.child_by_field_name("parameter")
            if parameter is not None and parameter.type == "identifier":
                self.blocked.add((node.id, self._text(parameter)))
            return
        elif kind == "global_statement":
            for child in node.named_children:
                if child.type == "identifier":
                    self.blocked.add((self.root.id, self._text(child)))
            return
        elif kind in {"assignment_expression", "augmented_assignment", "update_expression"}:
            name = node.child_by_field_name("left") or node.child_by_field_name("argument")
            if name is not None and name.type in _IDENTIFIERS:
                self.blocked.add(self._key(node, self._text(name)))
            return
        else:
            return

    # 先处理遮蔽，再查不可变声明，避免误用外层同名常量。
    # 前向引用与循环绑定均返回未知，不能靠遍历顺序补值。
    def _lookup(self, node: Any, depth: int, seen: frozenset[tuple[int, str]]) -> ValueSummary:
        name = self._text(node)
        scope = self._scope(node)
        while scope is not None:
            key = (scope.id, name)
            if key in self.blocked:
                return ValueSummary(reason="mutable-or-shadowed")
            declarations = self.declarations.get(key, ())
            if declarations:
                if len(declarations) != 1 or key in seen:
                    return ValueSummary(reason="ambiguous-or-cyclic-binding")
                expression = declarations[0]
                if expression.start_byte > node.start_byte:
                    return ValueSummary(reason="forward-binding")
                return self._evaluate(expression, depth + 1, seen | {key})
            scope = self._scope(scope) if scope != self.root else None
        return ValueSummary(reason="unbound-value")

    # 表达式解释有深度上限，语法错误节点不继续推断。
    # 字符串拼接只有两侧完整时才计算笛卡尔积，最终仍受值域上限约束。
    # 条件表达式必须两个分支都完整，不能只返回更容易理解的一侧。
    # literal_eval 只解释字面量，不执行函数调用或任意 Python 表达式。
    def _evaluate(
        self, node: Any, depth: int = 0, seen: frozenset[tuple[int, str]] = frozenset()
    ) -> ValueSummary:
        if depth >= MAX_EXPRESSION_DEPTH or node.has_error:
            return ValueSummary(reason="expression-limit-or-error")
        kind = node.type
        raw = self._text(node)
        environment = self._environment(node, raw)
        if environment is not None:
            return environment
        if kind in {
            "string",
            "string_literal",
            "interpreted_string_literal",
            "raw_string_literal",
            "template_string",
        }:
            try:
                if raw.startswith("`") and raw.endswith("`") and "${" not in raw:
                    return _known([raw[1:-1]], "literal")
                return _known([ast.literal_eval(raw)], "literal")
            except (ValueError, SyntaxError):
                return ValueSummary(reason="dynamic-string")
        if kind in _IDENTIFIERS:
            return self._lookup(node, depth, seen)
        if (
            kind in {"parenthesized_expression", "parenthesized_list"}
            and len(node.named_children) == 1
        ):
            return self._evaluate(node.named_children[0], depth + 1, seen)
        if kind in {"binary_expression", "binary_operator"}:
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None:
                operator = self.source[left.end_byte : right.start_byte].strip()
                if operator == b"+":
                    a, b = (
                        self._evaluate(left, depth + 1, seen),
                        self._evaluate(right, depth + 1, seen),
                    )
                    if a.complete and b.complete:
                        return _known((x + y for x in a.values for y in b.values), "concatenation")
                    return _symbolic("dynamic-concatenation", a, b)
        if kind in {"ternary_expression", "conditional_expression"}:
            branches = [
                node.child_by_field_name("consequence"),
                node.child_by_field_name("alternative"),
            ]
            if any(part is None for part in branches) and len(node.named_children) == 3:
                # Python orders consequence, condition, alternative.
                branches = (
                    [node.named_children[0], node.named_children[2]]
                    if self.suffix == ".py"
                    else list(node.named_children[1:])
                )
            if all(part is not None for part in branches):
                values = [self._evaluate(part, depth + 1, seen) for part in branches]
                if all(item.complete for item in values):
                    return _known((s for item in values for s in item.values), "conditional")
                return _symbolic("dynamic-conditional", *values)
        return ValueSummary(reason="unsupported-expression")

    # 环境访问器需验证可见导入来源，局部同名对象不能被误当作标准库。
    def _visible_origin(self, node: Any, name: str) -> str:
        scope = self._scope(node)
        while scope is not None:
            key = (scope.id, name)
            if key in self.blocked or key in self.declarations:
                return "shadowed"
            if key in self.import_origins:
                return self.import_origins[key]
            scope = self._scope(scope) if scope != self.root else None
        return ""

    # 按语法形状与导入绑定识别环境访问，不访问 os.environ。
    # JavaScript 的 process 被本地遮蔽时不生成环境符号。
    # 环境键也有长度限制，异常长表达式不会进入公共摘要。

    def _environment(self, node: Any, raw: str) -> ValueSummary | None:
        """Recognize syntax-bound environment reads without reading the environment."""
        name = ""
        accessor = ""
        if self.suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
            match = re.fullmatch(
                r"(process\.env|import\.meta\.env)(?:\.([A-Za-z_][A-Za-z_0-9]*)|\[\s*['\"]([^'\"]+)['\"]\s*\])",
                raw,
            )
            if match and not (match[1] == "process.env" and self._visible_origin(node, "process")):
                accessor, name = match[1], match[2] or match[3]
        elif node.type in _CALLS:
            accessor, name = self._environment_call(node, raw)
        if accessor and name and len(name) <= MAX_VALUE_LENGTH:
            return ValueSummary(
                reason="symbolic-environment", symbols=(SymbolReference(name, accessor),)
            )
        return None

    def _environment_call(self, node: Any, raw: str) -> tuple[str, str]:
        """按语言与可见导入识别调用，未知来源仍保留为普通表达式。"""
        accessor = ""
        name = ""
        match = re.fullmatch(r"([\w.]+)\(\s*['\"]([^'\"]+)['\"]\s*\)", raw)
        if match:
            expression, name = match[1], match[2]
            base, _, method = expression.partition(".")
            origin = self._visible_origin(node, base)
            if self.suffix == ".py" and (
                (origin == "os" and method == "getenv") or (origin == "os.getenv" and not method)
            ):
                accessor = "os.getenv"
            elif self.suffix == ".go" and origin == "os" and method == "Getenv":
                accessor = "os.Getenv"
            elif (
                self.suffix == ".java"
                and expression in {"System.getenv", "java.lang.System.getenv"}
                and (expression.startswith("java.lang.") or not origin)
            ):
                accessor = "java.lang.System.getenv"
        return accessor, name

    # 操作名仅用于缩小调用候选，精确位置仍是判断唯一调用的依据。
    def _call_name(self, call: Any) -> str:
        name = call.child_by_field_name("name") or call.child_by_field_name("function")
        if name is None:
            return ""
        leaf = (
            name.child_by_field_name("attribute")
            or name.child_by_field_name("property")
            or name.child_by_field_name("field")
        )
        raw = self._text(leaf if leaf is not None else name)
        return raw.rsplit(".", 1)[-1].rsplit("::", 1)[-1].split("(", 1)[0]

    # 优先用操作名、行和列精确定位，回退行匹配必须只有一个候选。
    # 不同提供者列编码先转换为 UTF-8 字节列，再查询语法位置。
    # UTF-16 截断到代理项中间会失败，不能继续使用错误列号。
    # 参数越界或调用歧义均保留未知原因，而不回退到其他参数。
    def argument(
        self,
        *,
        line: int,
        column: int,
        operation: str,
        index: int = 0,
        column_encoding: str = "utf8-byte",
    ) -> ValueSummary:
        if self.root is None:
            return ValueSummary(reason=self.reason)
        if line < 1 or column < 1 or index < 0:
            return ValueSummary(reason="invalid-call-position")
        operation = operation.split("(", 1)[0].rsplit(".", 1)[-1]
        line_candidates = self.call_lines.get((operation, line), ())
        byte_column: int | None = column if column_encoding == "utf8-byte" else None
        if column_encoding in {"utf16", "unicode-codepoint"}:
            at_line = next(
                (call for call in line_candidates if call.start_point.row + 1 == line), None
            )
            if at_line is not None:
                start = at_line.start_byte - at_line.start_point.column
                end = self.source.find(b"\n", start)
                raw_line = self.source[start : end if end >= 0 else len(self.source)].decode(
                    "utf-8"
                )
                try:
                    prefix = (
                        raw_line.encode("utf-16-le")[: (column - 1) * 2].decode("utf-16-le")
                        if column_encoding == "utf16"
                        else raw_line[: column - 1]
                    )
                    byte_column = len(prefix.encode("utf-8")) + 1
                except UnicodeError:
                    byte_column = None
        exact = (
            self.call_positions.get((operation, line, byte_column))
            if byte_column is not None
            else None
        )
        candidates = [exact] if exact is not None else line_candidates
        if len(candidates) != 1:
            return ValueSummary(reason="ambiguous-or-missing-call")
        arguments = candidates[0].child_by_field_name("arguments")
        if arguments is None or index >= len(arguments.named_children):
            return ValueSummary(reason="missing-argument")
        return self._evaluate(arguments.named_children[index])


# 源码读取沿用活跃快照，避免资源名与调用事实来自不同版本。
# 读取失败返回不可用值索引，上层仍可保留调用本身的证据。
def read_value_index(root: Path, relative: str) -> SourceValueIndex:
    """Read a bounded regular source file; a failed read is an unknown value."""
    from modus.knowledge_reader import BoundKnowledgeReadError
    from modus.machine.corpus.input_snapshot import read_bytes

    try:
        source = read_bytes(root, root / relative).decode("utf-8", "replace")
    except (BoundKnowledgeReadError, OSError, ValueError):
        source = ""
    return SourceValueIndex(source, Path(relative).suffix.lower())
