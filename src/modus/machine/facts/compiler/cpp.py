"""Clang command filtering, source anchors and compiler facts."""

from __future__ import annotations

import json
import os
import shlex
import shutil
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

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
from .process import _run_process


# 编译数据库是输入数据，原始命令不能作为 shell 字符串执行。
# 仅保留解析所需参数，输出、插件、响应文件与隐式配置入口被排除。
def _safe_compile_args(entry: Mapping[str, Any], source: Path) -> list[str]:
    """Strip a compile_commands.json entry to arguments safe for a read-only AST dump.

    Drops anything that would write output, load a plugin/module into the
    clang process, or otherwise exceed a pure parse-and-typecheck pass.
    """
    # 编译器身份由工具发现阶段决定，数据库首项仅为原命令名，不可作为执行入口。
    # arguments 优先于 command，后者只做分词，绝不交由 shell 求值。
    raw = entry.get("arguments")
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        args = list(raw[1:])
    elif isinstance(entry.get("command"), str):
        args = shlex.split(str(entry["command"]))[1:]
    else:
        args = []
    safe: list[str] = []
    skip_next = False
    forbidden_next = {
        "-o",
        "-MF",
        "-MT",
        "-MQ",
        "-include-pch",
        "-fmodule-file",
        # A compilation database is project-controlled input.  Never let it
        # load code into the Modus process (or its clang child) while all we
        # need is a read-only AST.  ``-Xclang`` always consumes the following
        # frontend argument, so dropping the pair also handles the usual
        # ``-Xclang -load -Xclang plugin.so`` spelling.
        "-Xclang",
        "-load",
        "-plugin",
        "-add-plugin",
        "-MJ",
        "--serialize-diagnostics",
        "-dependency-file",
        "--config",
        "-wrapper",
    }
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item in forbidden_next:
            skip_next = True
            continue
        if item in {"-c", "-S", "-E", "-MD", "-MMD", "-cc1"} or item == str(source):
            continue
        if item.startswith(
            (
                "-fplugin",
                "@",
                "-Wl,",
                "-Wa,",
                "-Wp,",
                "-MJ",
                "--serialize-diagnostics=",
                "-dependency-file=",
                "-fprofile",
                "-coverage",
                "--coverage",
                "-save-temps",
                "-ftime-trace",
                "-fmodules",
                "-fmodule-output",
            )
        ):
            continue
        if not item.startswith("-") and Path(item).suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}:
            continue
        safe.append(item)
    return safe


def _spelling_location(value: object) -> Mapping[str, Any]:
    """Unwrap a clang JSON location to its spelling location, if macro-expanded."""
    item = value if isinstance(value, Mapping) else {}
    nested = item.get("spellingLoc")
    return cast(Mapping[str, Any], nested if isinstance(nested, Mapping) else item)


# 隐式节点和无法确定包含来源的节点不制造源码位置。
# 只有没有显式跨文件证据时才继承父节点文件上下文。
def _clang_source_file(node: Mapping[str, Any], inherited: Path | None) -> Path | None:
    """Retain a declaration's file context without guessing an included file."""
    if node.get("isImplicit"):
        return None
    bounds = node.get("range")
    bounds = bounds if isinstance(bounds, Mapping) else {}
    locations = (
        _spelling_location(bounds.get("begin")),
        _spelling_location(node.get("loc")),
    )
    for location in locations:
        if isinstance(location.get("file"), str) and location["file"]:
            return Path(location["file"])
    if any(
        location.get("includedFrom")
        for location in (*locations, _spelling_location(bounds.get("end")))
    ):
        return None
    return inherited


# 宏展开优先使用拼写位置，跨文件范围不能表达为单一连续锚点。
# 字节偏移转换基于冻结源码，行起点缓存避免重复扫描。
# UTF-8 续字节不是合法字符边界，异常偏移直接拒绝。
# 结束位置按 token 长度转为开区间，与其他提供者的位置协议对齐。
def _clang_location(
    node: Mapping[str, Any],
    source: Path | None,
    root: Path,
    coordinates: dict[str, tuple[bytes, list[int]]] | None = None,
) -> SourceAnchor | None:
    from modus.machine.corpus.input_snapshot import read_bytes

    if node.get("isImplicit"):
        return None
    bounds = cast(
        Mapping[str, Any], node.get("range") if isinstance(node.get("range"), Mapping) else {}
    )
    location = _spelling_location(node.get("loc"))
    begin = _spelling_location(bounds.get("begin")) or location
    end = _spelling_location(bounds.get("end"))
    file = _clang_source_file(node, source)
    if file is None or not begin:
        return None
    file = file if file.is_absolute() else root / file
    try:
        resolved = file.resolve()
        rel = resolved.relative_to(root.resolve()).as_posix()
        # A range crossing files (for example through macro spelling) cannot
        # support one contiguous source occurrence.
        for point in (location, begin, end):
            explicit = point.get("file")
            if isinstance(explicit, str) and explicit:
                target = Path(explicit)
                target = target if target.is_absolute() else root / target
                if target.resolve() != resolved:
                    return None
    except (OSError, ValueError):
        return None
    cache = coordinates if coordinates is not None else {}
    if rel not in cache:
        try:
            raw = read_bytes(root, root / rel)
            starts = [0] + [index + 1 for index, value in enumerate(raw) if value == 10]
            cache[rel] = (raw, starts)
        except (OSError, ValueError):
            cache[rel] = (b"", [])
    raw, starts = cache[rel]

    def position(
        point: Mapping[str, Any], *, exclusive_end: bool = False
    ) -> tuple[int, int] | None:
        token_length = point.get("tokLen", 0) if exclusive_end else 0
        if type(token_length) is not int or token_length < 0:
            return None
        offset = point.get("offset")
        if offset is not None:
            if type(offset) is not int or not starts or not 0 <= offset + token_length <= len(raw):
                return None
            offset += token_length
            if offset < len(raw) and raw[offset] & 0xC0 == 0x80:
                return None
            line = bisect_right(starts, offset)
            return line, offset - starts[line - 1] + 1
        explicit_line, explicit_column = point.get("line"), point.get("col")
        if (
            type(explicit_line) is not int
            or type(explicit_column) is not int
            or explicit_line < 1
            or explicit_column < 1
        ):
            return None
        return explicit_line, explicit_column + token_length

    start = position(begin)
    if start is None:
        return None
    finish = position(end, exclusive_end=True) if end else None
    if finish is not None and finish < start:
        finish = None
    return SourceAnchor(
        file=rel,
        line=start[0],
        column=start[1],
        end_line=finish[0] if finish is not None else None,
        end_column=finish[1] if finish is not None else None,
        column_encoding="utf8-byte",
    )


def _walk_ast(node: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield *node* and every descendant in the clang JSON AST, depth-first."""
    yield node
    for child in node.get("inner", ()) or ():
        if isinstance(child, Mapping):
            yield from _walk_ast(child)


# 先收集声明再处理调用，引用可解析到后出现的声明。
# 编译器临时 ID 仅在本次 AST 内使用，公开身份取稳定符号信息。
# 虚调用的候选扩展保留不确定性，不把相同签名当作运行时类型证明。
def _cpp_facts_for_ast(
    ast_root: Mapping[str, Any], source: Path, root: Path, unit: BuildUnit
) -> SemanticFacts:
    """Extract declarations and call edges from one clang JSON AST dump.

    Virtual dispatch is over-approximated conservatively: every call through
    an overridden virtual method also emits ``candidate`` edges to every
    other override sharing its name and signature, since clang's AST alone
    cannot prove the dynamic type at the call site.
    """
    provenance = Provenance("cpp-clang", "1", unit.context_digest)
    declaration_kinds = {"FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl"}
    nodes: dict[str, SemanticNode] = {}
    clang_ids: dict[str, str] = {}
    coordinates: dict[str, tuple[bytes, list[int]]] = {}

    # 优先使用编译器修饰名区分重载，缺失时回退到限定名与签名。
    def declaration_identity(item: Mapping[str, Any], owners: tuple[str, ...]) -> tuple[str, str]:
        name = str(item.get("name") or "anonymous")
        qualified = str(item.get("qualifiedName") or "::".join((*owners, name)))
        signature = str((item.get("type") or {}).get("qualType") or "")
        symbol = str(item.get("mangledName") or qualified + signature)
        return f"cpp:{symbol}", qualified + signature

    # 前向声明与定义合并源码出现位置，不能以后写记录丢弃之前的锚点。
    def collect(
        item: Mapping[str, Any], owners: tuple[str, ...] = (), owning_file: Path | None = source
    ) -> None:
        owning_file = _clang_source_file(item, owning_file)
        kind = str(item.get("kind") or "")
        next_owners = owners
        if kind == "NamespaceDecl" and item.get("name"):
            next_owners = (*owners, str(item["name"]))
        elif kind == "CXXRecordDecl" and item.get("name"):
            type_name = "::".join((*owners, str(item["name"])))
            next_owners = (*owners, str(item["name"]))
            if item.get("completeDefinition") or item.get("definitionData"):
                anchor = _clang_location(item, owning_file, root, coordinates)
                nodes[f"cpp:{type_name}"] = SemanticNode(
                    id=f"cpp:{type_name}",
                    kind="type",
                    language="cpp",
                    qualified_name=type_name,
                    build_unit_id=unit.id,
                    source_anchors=(anchor,) if anchor is not None else (),
                    attributes={"owner": "::".join(owners)},
                    provenance=provenance,
                )
        if kind in declaration_kinds:
            node_id, qualified = declaration_identity(item, owners)
            name = str(item.get("name") or "anonymous")
            signature = str((item.get("type") or {}).get("qualType") or "")
            overrides = any(
                isinstance(child, Mapping) and child.get("kind") == "OverrideAttr"
                for child in item.get("inner", ()) or ()
            )
            clang_ids[str(item.get("id") or "")] = node_id
            anchor = _clang_location(item, owning_file, root, coordinates)
            previous_node = nodes.get(node_id)
            nodes[node_id] = SemanticNode(
                id=node_id,
                kind="method" if "CXX" in kind else "function",
                language="cpp",
                qualified_name=qualified,
                build_unit_id=unit.id,
                source_anchors=(anchor,) if anchor is not None else (),
                attributes={
                    "mangledName": item.get("mangledName", ""),
                    "owner": "::".join(owners),
                    "name": name,
                    "signature": signature,
                    "virtual": bool(item.get("virtual") or item.get("pure") or overrides),
                },
                provenance=provenance,
            )
            if previous_node is not None:
                # Forward declarations and definitions are one provider symbol
                # with several source occurrences, not last-writer replacement.
                combined = tuple(
                    dict.fromkeys((*previous_node.source_anchors, *nodes[node_id].source_anchors))
                )
                nodes[node_id] = replace(
                    nodes[node_id],
                    source_anchors=combined,
                    attributes={
                        **nodes[node_id].attributes,
                        "virtual": bool(
                            previous_node.attributes.get("virtual")
                            or nodes[node_id].attributes.get("virtual")
                        ),
                    },
                )
        for child in item.get("inner", ()) or ():
            if isinstance(child, Mapping):
                collect(child, next_owners, owning_file)

    collect(ast_root)
    edges: dict[str, SemanticEdge] = {}
    virtual_candidates: dict[tuple[str, str], list[str]] = {}
    for node_id, node in nodes.items():
        if node.kind != "method" or node.attributes.get("virtual") is not True:
            continue
        key = (
            str(node.attributes.get("name") or ""),
            str(node.attributes.get("signature") or ""),
        )
        virtual_candidates.setdefault(key, []).append(node_id)

    def find_reference(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
        referenced = item.get("referencedDecl")
        if isinstance(referenced, Mapping):
            return referenced
        referenced_id = item.get("referencedMemberDecl")
        if isinstance(referenced_id, str):
            return {"id": referenced_id, "name": item.get("name", "")}
        for child in item.get("inner", ()) or ():
            if isinstance(child, Mapping):
                found = find_reference(child)
                if found is not None:
                    return found
        return None

    # 缺少源码位置时用 AST 路径区分调用出现，避免所有未知调用合并。
    # 外部声明占位保留目标信息，但不伪造项目内源码。
    def calls(
        item: Mapping[str, Any],
        caller: str = "",
        owning_file: Path | None = source,
        ast_path: tuple[int, ...] = (),
    ) -> None:
        owning_file = _clang_source_file(item, owning_file)
        kind = str(item.get("kind") or "")
        active = (
            clang_ids.get(str(item.get("id") or ""), caller)
            if kind in declaration_kinds
            else caller
        )
        if (
            kind in {"CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr", "CXXConstructExpr"}
            and active
        ):
            reference = find_reference(item)
            if reference is not None:
                target = clang_ids.get(str(reference.get("id") or ""))
                if target is None:
                    qname = str(
                        reference.get("qualifiedName") or reference.get("name") or "unresolved"
                    )
                    signature = str((reference.get("type") or {}).get("qualType") or "")
                    target = f"cpp:{qname}{signature}"
                    nodes.setdefault(
                        target,
                        SemanticNode(
                            id=target,
                            kind="method" if kind == "CXXMemberCallExpr" else "function",
                            language="cpp",
                            qualified_name=qname + signature,
                            build_unit_id=unit.id,
                            attributes={"externalDeclaration": True},
                            provenance=provenance,
                        ),
                    )
                anchor = _clang_location(item, owning_file, root, coordinates)
                occurrence = (
                    (anchor.file, anchor.line, anchor.column, anchor.end_line, anchor.end_column)
                    if anchor is not None
                    else ("unknown-source", *ast_path)
                )
                edge_id = stable_id("cpp-edge", active, target, *occurrence)
                target_node = nodes[target]
                is_virtual = target_node.attributes.get("virtual") is True
                edges[edge_id] = SemanticEdge(
                    id=edge_id,
                    source=active,
                    target=target,
                    relation="calls",
                    resolution="exact" if reference.get("id") else "candidate",
                    operation=str(reference.get("name") or ""),
                    call_site=anchor,
                    dispatch=(
                        "virtual"
                        if kind == "CXXMemberCallExpr" and is_virtual
                        else "method"
                        if kind == "CXXMemberCallExpr"
                        else "static"
                    ),
                    evidence=("clang typed AST declaration reference",),
                    provenance=provenance,
                    binding_kind=(
                        "runtime-candidate"
                        if kind == "CXXMemberCallExpr" and is_virtual
                        else "runtime-exact"
                        if reference.get("id")
                        else "runtime-candidate"
                    ),
                )
                if kind == "CXXMemberCallExpr" and is_virtual:
                    candidate_key = (
                        str(target_node.attributes.get("name") or ""),
                        str(target_node.attributes.get("signature") or ""),
                    )
                    for candidate in sorted(virtual_candidates.get(candidate_key, ())):
                        if candidate == target:
                            continue
                        candidate_id = stable_id(
                            "cpp-edge-candidate",
                            active,
                            candidate,
                            *occurrence,
                        )
                        edges[candidate_id] = SemanticEdge(
                            id=candidate_id,
                            source=active,
                            target=candidate,
                            relation="calls",
                            resolution="candidate",
                            operation=str(reference.get("name") or ""),
                            call_site=anchor,
                            dispatch="virtual",
                            evidence=("clang virtual override candidate",),
                            attributes={"declarationTarget": target},
                            provenance=provenance,
                            binding_kind="runtime-candidate",
                        )
        for index, child in enumerate(item.get("inner", ()) or ()):
            if isinstance(child, Mapping):
                calls(child, active, owning_file, (*ast_path, index))

    calls(ast_root)
    return SemanticFacts(tuple(nodes.values()), tuple(edges.values()))


# 每个翻译单元独立调用只读 AST 模式，单元失败产生诊断后继续。
# 只处理仓库内选定的源码，遵守构建单元捕获的变体。
# 移除会覆盖 Clang 选项的环境变量，保证参数过滤结果有效。
# 输出先校验为 JSON 对象，再进入声明与调用提取。
def _run_cpp(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> SemanticFacts:
    """Dump a typed AST per translation unit with clang and extract compiler facts.

    *config* carries no C++-specific settings today; it is accepted for
    signature parity with the other language providers.
    """
    del config
    clang = shutil.which("clang++") or shutil.which("clang")
    database_path = str(unit.metadata.get("compilationDatabase") or "")
    if not clang or not database_path:
        raise CompilerSemanticError("clang or compile_commands.json is unavailable")
    database = _compilation_database(unit, database_path)
    all_nodes: dict[str, SemanticNode] = {}
    all_edges: dict[str, SemanticEdge] = {}
    diagnostics: list[SemanticDiagnostic] = []
    for entry in database:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("file"), str):
            continue
        selected_source = _translation_unit_source(entry, unit, paths.root)
        if selected_source is None:
            continue
        directory, source = selected_source
        argv = [
            clang,
            *_safe_compile_args(entry, source),
            "-fsyntax-only",
            "-Xclang",
            "-ast-dump=json",
            str(source),
        ]
        env = dict(os.environ)
        for name in ("CCC_OVERRIDE_OPTIONS", "CLANG_CONFIG_FILE"):
            env.pop(name, None)
        completed = _run_process(argv, cwd=directory, env=env, timeout=300)
        if completed.returncode != 0:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.cpp-translation-unit-failed",
                    message=completed.stderr.strip()[-2000:],
                    severity="warning",
                    provider="cpp-clang",
                    build_unit_id=unit.id,
                    subject=source.relative_to(paths.root).as_posix(),
                )
            )
            continue
        try:
            ast_root = json.loads(completed.stdout)
            if not isinstance(ast_root, Mapping):
                raise ProviderError("machine.facts.cpp-invalid-ast")
        except (json.JSONDecodeError, ValueError) as exc:
            diagnostics.append(
                SemanticDiagnostic(
                    "machine.facts.cpp-translation-unit-failed",
                    safe_error_summary(exc),
                    "warning",
                    "cpp-clang",
                    unit.id,
                    source.relative_to(paths.root).as_posix(),
                )
            )
            continue
        facts = _cpp_facts_for_ast(ast_root, source, paths.root, unit)
        all_nodes.update({item.id: item for item in facts.nodes})
        all_edges.update({item.id: item for item in facts.edges})
    if not all_nodes:
        raise CompilerSemanticError("Clang provider emitted no declarations")
    return SemanticFacts(tuple(all_nodes.values()), tuple(all_edges.values()), tuple(diagnostics))


def _translation_unit_source(
    entry: Mapping[str, Any], unit: BuildUnit, root: Path
) -> tuple[Path, Path] | None:
    """只选择当前构建单元与仓库边界内的源文件，保留命令中的原始路径。"""
    directory = Path(str(entry.get("directory") or unit.root)).resolve()
    source = Path(str(entry["file"]))
    if not source.is_absolute():
        source = directory / source
    selected = str(unit.metadata.get("translationUnit") or "")
    if selected and source.resolve() != Path(selected).resolve():
        return None
    try:
        source.resolve().relative_to(root)
    except ValueError:
        return None
    return directory, source


# 冻结的 commandEntry 优先，避免活跃数据库变化切换编译变体。
# 回退读取也必须符合数组结构，不能把任意 JSON 当作编译命令集。
# 命令读取失败属于提供者失败，由上层决定降级或终止。
def _compilation_database(unit: BuildUnit, database_path: str) -> list[Any]:
    """Prefer the frozen command entry; validate a fallback database's shape.

    A selected variant must not be replaced by another entry for the same source
    just because the live compilation database contains multiple commands.
    """
    try:
        command_entry = unit.metadata.get("commandEntry")
        database = (
            [command_entry]
            if isinstance(command_entry, Mapping)
            else json.loads(Path(database_path).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise CompilerSemanticError(f"could not read compilation database: {exc}") from exc
    if not isinstance(database, list):
        raise CompilerSemanticError("compilation database must be a JSON array")
    return database
