"""Local Java provider and classpath assembly."""

from __future__ import annotations

import os
from dataclasses import replace

from modus.config import ModusConfig
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.semantic_ir import SemanticDiagnostic, SemanticFacts, parse_jsonl
from modus.machine.runtime.toolchains import java_executable
from modus.paths import ModusPaths

from .models import CompilerSemanticError
from .process import _asset, _compile_asset, _java_env, _java_tools_jar, _run_process, _source_list


# 已解析 classpath 优先，避免在执行阶段重新作不同的构建发现。
# 显式 classpath 文件必须位于仓库内并遵守大小预算。
# 读取通过冻结输入接口，保证用于分析的内容参与基线校验。
# 追加已有输出目录不代表运行项目构建，缺失依赖由提供器诊断。
def _java_classpath(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> str:
    """Resolve the classpath for *unit*: an explicit file, then common build output dirs."""
    if "resolvedClasspath" in unit.metadata:
        return str(unit.metadata["resolvedClasspath"])
    from modus.machine.corpus.input_snapshot import read_bytes

    configured = config.init_analysis.compiler.java.classpath_file
    entries: list[str] = []
    generated = str(unit.metadata.get("classpathFile") or "")
    classpath_file = configured or generated
    if classpath_file:
        target = (paths.root / classpath_file).resolve()
        try:
            target.relative_to(paths.root)
        except ValueError as exc:
            raise CompilerSemanticError(
                "Java classpath_file must stay inside the repository"
            ) from exc
        if not target.is_file() or target.stat().st_size > 8 * 1024 * 1024:
            raise CompilerSemanticError("Java classpath_file is missing or exceeds 8 MiB")
        entries.extend(
            item
            for item in read_bytes(paths.root, target).decode("utf-8").strip().split(os.pathsep)
            if item
        )
    for pattern in ("**/target/classes", "**/build/classes/java/main"):
        entries.extend(str(item.resolve()) for item in paths.root.glob(pattern) if item.is_dir())
    return os.pathsep.join(dict.fromkeys(entries))


# 编译的是随包 javac API 分析器，项目文件通过源清单传入。
# 提供器自身 classpath 与被分析项目 classpath 分开，避免依赖混淆。
# 参数逐项传递给受限进程边界，不拼装 shell 字符串。
# 静态调用可保持精确，虚调用和接口调用必须保留候选分派语义。
# 非空声明是提供器成功的最低契约，零退出码不能单独证明有效结果。
# stderr 保留为有界编译诊断，不混入 JSONL 标准输出。
def _run_java(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> SemanticFacts:
    """Compile the bundled javac-API analyzer once, then run it against *unit*."""
    javac = java_executable("javac")
    java = java_executable("java")
    if not javac or not java:
        raise CompilerSemanticError("javac/java is unavailable")
    classes = _compile_asset(
        paths,
        language="java",
        source=_asset("ModusJavaAnalyzer.java"),
        compiler=javac,
    )
    request = _source_list(paths, unit)
    tools_jar = _java_tools_jar(javac)
    provider_classpath = os.pathsep.join(
        [str(classes), *([str(tools_jar)] if tools_jar is not None else [])]
    )
    completed = _run_process(
        [
            java,
            "-Xmx2g",
            "-cp",
            provider_classpath,
            "ModusJavaAnalyzer",
            str(paths.root),
            unit.id,
            unit.context_digest,
            str(request),
            _java_classpath(paths, config, unit),
            str(config.init_analysis.compiler.java.annotation_processing).lower(),
        ],
        cwd=unit.root,
        env=_java_env(),
        timeout=1800,
    )
    if completed.returncode != 0:
        raise CompilerSemanticError("Javac provider failed: " + completed.stderr.strip()[-4000:])
    facts = parse_jsonl(completed.stdout.splitlines())
    if not facts.nodes:
        raise CompilerSemanticError("Javac provider emitted no declarations")
    normalized_edges = tuple(
        replace(
            edge,
            binding_kind=(
                "runtime-exact"
                if edge.relation == "calls"
                and edge.resolution == "exact"
                and edge.dispatch == "static"
                else "runtime-candidate"
                if edge.relation == "calls" and edge.dispatch in {"interface", "virtual"}
                else edge.binding_kind
            ),
        )
        for edge in facts.edges
    )
    diagnostics = list(facts.diagnostics)
    if completed.stderr.strip():
        diagnostics.append(
            SemanticDiagnostic(
                code="machine.facts.java-compiler-diagnostics",
                message=completed.stderr.strip()[-4000:],
                severity="warning",
                provider="java-javac",
                build_unit_id=unit.id,
            )
        )
    return replace(facts, edges=normalized_edges, diagnostics=tuple(diagnostics))
