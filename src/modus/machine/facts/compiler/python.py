"""Python declaration/call facts and optional local SCIP enhancement."""

from __future__ import annotations

import ast
import importlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from modus.config import ModusConfig
from modus.machine.errors import ProviderError, safe_error_summary
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.semantic_ir import (
    Provenance,
    SemanticDiagnostic,
    SemanticEdge,
    SemanticFacts,
    SemanticNode,
    SourceAnchor,
    stable_id,
)
from modus.paths import ModusPaths

from .models import CompilerSemanticError
from .process import _MAX_PROVIDER_OUTPUT, _run_process

_MAX_PYTHON_SOURCE_FILE = 8 * 1024 * 1024


_MAX_PYTHON_SOURCE_TOTAL = 64 * 1024 * 1024


def _source_text(root: Path, source: Path) -> str:
    """Read *source* through the bound reader and decode it as UTF-8."""
    from modus.machine.corpus.input_snapshot import read_bytes

    return read_bytes(root, source).decode("utf-8")


# 模块身份兼容 src 布局与包初始化文件，不把 __init__ 当作独立业务模块。
def _python_module_name(root: Path, source: Path) -> str:
    """Derive a dotted module name from *source*'s path, honoring a ``src/`` layout."""
    relative = source.relative_to(root)
    parts = list(relative.with_suffix("").parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1 :]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or source.stem


# Python AST 列偏移按 UTF-8 字节计数，公开位置显式标明编码。
def _python_anchor(root: Path, source: Path, node: ast.AST) -> SourceAnchor:
    """Build a 1-based source anchor from an AST node's line/column attributes."""
    return SourceAnchor(
        file=source.relative_to(root).as_posix(),
        line=max(1, int(getattr(node, "lineno", 1))),
        column=max(1, int(getattr(node, "col_offset", 0)) + 1),
        end_line=max(1, int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))),
        end_column=max(
            1,
            int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))) + 1,
        ),
        column_encoding="utf8-byte",
    )


# 词法绑定能力与完整运行时调度不同，动态属性调用保留候选状态。
# 作用域栈分别跟踪拥有者、调用者与遮蔽名称，避免类和函数语义混用。
# 跨文件已知声明索引只提供可证明的名称目标，不执行被分析代码。
class _PythonFactsBuilder(ast.NodeVisitor):
    """Walk one module's AST, emitting declaration nodes and lexically-bound call edges.

    Resolution is purely lexical: a call target is ``declared-exact`` only
    when the callee resolves, through imports and local scoping, to a name
    in the cross-file ``known`` declaration index; anything else is
    ``runtime-candidate`` (attribute calls) or ``unresolved``.
    """

    def __init__(
        self,
        *,
        root: Path,
        source: Path,
        module: str,
        unit: BuildUnit,
        known: Mapping[str, str],
    ) -> None:
        self.root = root
        self.source = source
        self.module = module
        self.unit = unit
        self.known = known
        self.provenance = Provenance("python-builtin", "2", unit.context_digest)
        self.nodes: dict[str, SemanticNode] = {}
        self.edges: dict[str, SemanticEdge] = {}
        self.owners: list[str] = []
        self.owner_kinds: list[str] = []
        self.callers: list[str] = []
        self.imports: dict[str, str] = {}
        self.shadowed: list[set[str]] = []

    def visit_Module(self, node: ast.Module) -> None:
        self.shadowed.append(_lexical_writes(node, include_declarations=False))
        self.generic_visit(node)
        self.shadowed.pop()

    def _qualified(self, name: str) -> str:
        return ".".join((self.module, *self.owners, name))

    def _node(self, node: ast.AST, *, name: str, kind: str) -> str:
        qualified = self._qualified(name)
        symbol_id = (
            f"python {self.module} {'/'.join((*self.owners, name))}"
            f"{'().' if kind in {'function', 'method'} else '#'}"
        )
        node_id = stable_id("python-node", symbol_id)
        self.nodes[node_id] = SemanticNode(
            id=node_id,
            kind=kind,
            language="python",
            qualified_name=qualified,
            build_unit_id=self.unit.id,
            source_anchors=(_python_anchor(self.root, self.source, node),),
            attributes={"name": name, "module": self.module, "owner": ".".join(self.owners)},
            provenance=self.provenance,
            symbol_id=symbol_id,
        )
        return node_id

    # 类体导入不是方法的闭包环境，不能直接用于方法中的名称解析。
    def visit_Import(self, node: ast.Import) -> None:
        if self.owner_kinds and self.owner_kinds[-1] == "type":
            return  # A method does not close over its class's import namespace.
        for alias in node.names:
            self.imports[alias.asname or alias.name.split(".")[0]] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.owner_kinds and self.owner_kinds[-1] == "type":
            return
        prefix = node.module or ""
        for alias in node.names:
            self.imports[alias.asname or alias.name] = ".".join(
                item for item in (prefix, alias.name) if item
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        node_id = self._node(node, name=node.name, kind="type")
        self.owners.append(node.name)
        self.owner_kinds.append("type")
        for child in node.body:
            self.visit(child)
        self.owners.pop()
        self.owner_kinds.pop()
        del node_id

    # 进入函数时保存导入环境，离开后恢复，防止局部导入泄漏到兄弟函数。
    # 局部写入预先收集，调用发生在赋值前也不能错误绑定到全局同名符号。
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.owner_kinds and self.owner_kinds[-1] == "type" else "function"
        node_id = self._node(node, name=node.name, kind=kind)
        self.owners.append(node.name)
        self.owner_kinds.append("function")
        self.callers.append(node_id)
        self.shadowed.append(_lexical_writes(node, include_declarations=True))
        parent_imports = dict(self.imports)
        for child in node.body:
            self.visit(child)
        self.callers.pop()
        self.owners.pop()
        self.owner_kinds.pop()
        self.shadowed.pop()
        self.imports = parent_imports

    visit_AsyncFunctionDef = visit_FunctionDef

    # 未找到目标时保留外部声明占位，避免丢失调用观察。
    # 调用位置参与边身份，同一对函数的多个调用点不会合并。
    # 动态调用不因目标名称相同就升级为精确运行时绑定。
    def visit_Call(self, node: ast.Call) -> None:
        if self.callers:
            target_name, binding = self._call_target(node.func)
            target = self.known.get(target_name, "")
            if not target:
                target = stable_id("python-external", target_name or "unresolved")
                self.nodes.setdefault(
                    target,
                    SemanticNode(
                        id=target,
                        kind="function",
                        language="python",
                        qualified_name=target_name or "<unresolved>",
                        build_unit_id=self.unit.id,
                        attributes={"externalDeclaration": True},
                        provenance=self.provenance,
                        symbol_id=f"python external {target_name or 'unresolved'}",
                        analysis_completeness="partial",
                    ),
                )
            anchor = _python_anchor(self.root, self.source, node)
            resolution = {
                "declared-exact": "exact",
                "runtime-candidate": "candidate",
                "unresolved": "unresolved",
            }[binding]
            edge_id = stable_id(
                "python-edge",
                self.callers[-1],
                target,
                anchor.file,
                anchor.line,
                anchor.column,
                anchor.end_line,
                anchor.end_column,
            )
            self.edges[edge_id] = SemanticEdge(
                id=edge_id,
                source=self.callers[-1],
                target=target,
                relation="calls",
                resolution=resolution,
                operation=target_name.rsplit(".", 1)[-1] if target_name else "",
                call_site=anchor,
                dispatch="dynamic" if isinstance(node.func, ast.Attribute) else "direct",
                evidence=("Python AST lexical binding",),
                provenance=self.provenance,
                binding_kind=binding,
                analysis_completeness="partial" if binding != "declared-exact" else "complete",
            )
        self.generic_visit(node)

    # 局部变量遮蔽优先于导入和模块声明，不能跨过词法作用域猜测目标。
    # 属性链只提供候选限定名，接收者实际类型仍可能在运行时变化。
    def _call_target(self, value: ast.expr) -> tuple[str, str]:
        if isinstance(value, ast.Name):
            if any(value.id in names for names in self.shadowed):
                return self._qualified("<local>." + value.id), "unresolved"
            imported = self.imports.get(value.id)
            if imported:
                return imported, "declared-exact" if imported in self.known else "unresolved"
            local = ".".join((self.module, value.id))
            return local, "declared-exact" if local in self.known else "unresolved"
        if isinstance(value, ast.Attribute):
            parts: list[str] = [value.attr]
            owner: ast.expr = value.value
            while isinstance(owner, ast.Attribute):
                parts.append(owner.attr)
                owner = owner.value
            if isinstance(owner, ast.Name):
                if any(owner.id in names for names in self.shadowed):
                    return self._qualified("<local>." + owner.id + "." + value.attr), "unresolved"
                parts.append(self.imports.get(owner.id, owner.id))
                return ".".join(reversed(parts)), "runtime-candidate"
            return value.attr, "runtime-candidate"
        return "<dynamic-call>", "unresolved"


# 嵌套函数体的赋值不影响外层绑定，只登记其声明名称。
# global 声明从局部遮蔽集合移除，参数则始终属于函数作用域。
def _lexical_writes(node: ast.AST, *, include_declarations: bool) -> set[str]:
    """Writes are scoped; nested bodies cannot rewrite an enclosing binding."""
    result: set[str] = set()
    globals_: set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = node.args
        result.update(
            arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        )
        result.update(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg)
    pending = list(getattr(node, "body", ()))
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Global):
            globals_.update(current.names)
        elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if include_declarations:
                result.add(current.name)
        elif isinstance(current, ast.Lambda):
            continue
        else:
            if isinstance(current, ast.Name) and isinstance(current.ctx, (ast.Store, ast.Del)):
                result.add(current.id)
            pending.extend(ast.iter_child_nodes(current))
    return result - globals_


# 先索引再访问调用，支持前向声明和跨文件引用。
# 单文件不可解析不终止索引，正式提取阶段负责记录该输入的诊断。
def _python_declaration_index(root: Path, files: tuple[Path, ...]) -> dict[str, str]:
    """Pre-index top-level and class-member declarations across *files* for call resolution."""
    result: dict[str, str] = {}
    for source in files:
        module = _python_module_name(root, source)
        try:
            tree = ast.parse(_source_text(root, source), filename=str(source))
        except (OSError, ValueError, SyntaxError, UnicodeError):
            continue
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = f"{module}.{item.name}"
                symbol = f"python {module} {item.name}{'().' if not isinstance(item, ast.ClassDef) else '#'}"
                result[qualified] = stable_id("python-node", symbol)
            if isinstance(item, ast.ClassDef):
                for member in item.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbol = f"python {module} {item.name}/{member.name}()."
                        result[f"{module}.{item.name}.{member.name}"] = stable_id(
                            "python-node", symbol
                        )
    return result


# SCIP 为可选符号增强，适配器不可用时保留基础 AST 事实。
# 索引有输出大小上限，避免外部产物无界加载。
# 只按精确源码坐标补充符号身份，不凭相似名称覆盖原有声明。
def _enhance_python_with_scip(index_path: Path, facts: SemanticFacts) -> SemanticFacts:
    """Apply stable SCIP symbols when the optional protobuf adapter is installed."""
    try:
        if index_path.stat().st_size > _MAX_PROVIDER_OUTPUT:
            raise ProviderError("machine.facts.python-index-too-large")
        module = importlib.import_module("scip_pb2")
        index = module.Index()
        from modus.machine.corpus.input_snapshot import active_snapshot, read_bytes

        snapshot = active_snapshot()
        index.ParseFromString(
            read_bytes(snapshot.root if snapshot else index_path.parent, index_path)
        )
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        diagnostic = SemanticDiagnostic(
            code="machine.facts.python-scip-adapter-unavailable",
            message=safe_error_summary(exc),
            severity="warning",
            provider="python-builtin",
        )
        return SemanticFacts(facts.nodes, facts.edges, (*facts.diagnostics, diagnostic))
    symbols: dict[tuple[str, int, int], str] = {}
    for document in getattr(index, "documents", ()):
        relative = str(getattr(document, "relative_path", "")).replace("\\", "/")
        for occurrence in getattr(document, "occurrences", ()):
            source_range = list(getattr(occurrence, "range", ()))
            symbol = str(getattr(occurrence, "symbol", ""))
            if symbol and len(source_range) >= 2:
                symbols[(relative, int(source_range[0]) + 1, int(source_range[1]) + 1)] = symbol
    nodes = tuple(
        replace(
            node,
            symbol_id=symbols.get(
                (
                    node.source_anchors[0].file,
                    node.source_anchors[0].line,
                    node.source_anchors[0].column,
                ),
                node.symbol_id,
            ),
        )
        if node.source_anchors
        else node
        for node in facts.nodes
    )
    return replace(facts, nodes=nodes)


# 输入同时受单文件与构建单元总量约束，限制 AST 解析资源。
# 失败文件有独立诊断，其他可解析文件仍能产生事实。
# 全部文件失败时抛出提供者错误，不能把空集报告为分析成功。
def _run_python(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> SemanticFacts:
    """Analyze *unit* with the built-in AST provider, then optionally enhance with SCIP."""
    from modus.machine.corpus.input_snapshot import read_bytes

    sizes = []
    for source in unit.files:
        try:
            sizes.append(len(read_bytes(paths.root, source)))
        except (OSError, ValueError):
            pass  # The per-file parse phase below records this failed input.
    if any(size > _MAX_PYTHON_SOURCE_FILE for size in sizes):
        raise CompilerSemanticError("Python source file exceeds 8 MiB provider limit")
    if sum(sizes) > _MAX_PYTHON_SOURCE_TOTAL:
        raise CompilerSemanticError("Python build unit exceeds 64 MiB provider input limit")
    known = _python_declaration_index(paths.root, unit.files)
    nodes: dict[str, SemanticNode] = {}
    edges: dict[str, SemanticEdge] = {}
    diagnostics: list[SemanticDiagnostic] = []
    parsed_files = 0
    for source in unit.files:
        try:
            tree = ast.parse(_source_text(paths.root, source), filename=str(source))
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.python-parse-failed",
                    message=safe_error_summary(exc),
                    severity="warning",
                    provider="python-builtin",
                    build_unit_id=unit.id,
                    subject=source.relative_to(paths.root).as_posix(),
                )
            )
            continue
        parsed_files += 1
        builder = _PythonFactsBuilder(
            root=paths.root,
            source=source,
            module=_python_module_name(paths.root, source),
            unit=unit,
            known=known,
        )
        builder.visit(tree)
        nodes.update(builder.nodes)
        edges.update(builder.edges)
    if parsed_files == 0:
        raise CompilerSemanticError("Python builtin provider could not parse any source file")
    facts = SemanticFacts(tuple(nodes.values()), tuple(edges.values()), tuple(diagnostics))
    return _enhance_python_provider(paths, config, unit, facts)


# 本地工具输出写临时目录，原项目只提供读取输入。
# 显式索引在本地索引之后增强，沿用既有优先级。
# required 模式下增强失败必须终止，不能用基础结果掩盖强制要求。
def _enhance_python_provider(
    paths: ModusPaths, config: ModusConfig, unit: BuildUnit, facts: SemanticFacts
) -> SemanticFacts:
    """Enhance builtin facts with optional local or supplied SCIP evidence.

    A local index is read before an explicit index, preserving precedence and
    accumulated diagnostics. A required adapter still fails after enhancement.
    """
    index_text = str(unit.metadata.get("scipIndex") or "")
    local_scip = str(unit.metadata.get("scipExecutable") or "")
    if local_scip:
        with tempfile.TemporaryDirectory(prefix="modus-scip-python-") as output_dir:
            output_root = Path(output_dir)
            env = dict(os.environ)
            env.update({"PIP_NO_INDEX": "1", "NO_PROXY": "*"})
            completed = _run_process(
                [
                    local_scip,
                    "index",
                    str(unit.root),
                    f"--project-name={paths.root.name}",
                ],
                cwd=output_root,
                env=env,
                timeout=1800,
            )
            generated = output_root / "index.scip"
            if completed.returncode == 0 and generated.is_file():
                facts = _enhance_python_with_scip(generated, facts)
            else:
                diagnostic = SemanticDiagnostic(
                    code="machine.facts.python-scip-local-failed",
                    message=(completed.stderr or completed.stdout or "index.scip was not produced")[
                        -4000:
                    ],
                    severity="warning",
                    provider="python-builtin",
                    build_unit_id=unit.id,
                )
                facts = replace(facts, diagnostics=(*facts.diagnostics, diagnostic))
    if index_text:
        facts = _enhance_python_with_scip(paths.root / index_text, facts)
    if config.init_analysis.compiler.python.provider == "scip" and any(
        item.code == "machine.facts.python-scip-adapter-unavailable"
        or item.code == "machine.facts.python-scip-local-failed"
        for item in facts.diagnostics
    ):
        raise CompilerSemanticError("required Python SCIP adapter is unavailable")
    return facts
