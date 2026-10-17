"""Compiler orchestration: preserve mode selection, provider ordering and degradation policy."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from modus.config import ModusConfig
from modus.machine.corpus.source_inventory import build_source_inventory
from modus.machine.errors import ConfigurationError, ProviderError, safe_error_summary
from modus.machine.facts.build_context import BuildContextResolver
from modus.machine.facts.build_contracts import BuildContext, BuildUnit
from modus.machine.facts.compiler.cache import _facts_path, _load_facts, _write_facts
from modus.machine.facts.compiler.merge import (
    _apply_source_dispositions,
    _merge_facts,
    receipt_facts,
    scope_facts,
)
from modus.machine.facts.compiler.models import CompilerSemanticError as CompilerSemanticError
from modus.machine.facts.compiler.models import CompilerSemanticResult as CompilerSemanticResult
from modus.machine.facts.semantic_ir import SemanticDiagnostic, SemanticFacts
from modus.machine.languages import PROVIDER_VERSIONS
from modus.paths import ModusPaths

if TYPE_CHECKING:
    from modus.machine.facts.extension_registry import ExtensionRegistry


# 兼容入口延迟加载具体提供器，公共协调层不承担 TS 运行实现。
def _run_typescript(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> SemanticFacts:
    from modus.machine.facts.compiler.typescript import _run_typescript as runner

    return runner(paths, config, unit)


# 模式决定失败策略：off 不做工具探测，auto 保留可用观察，required 要求成功。
# 注册表可注入，但默认实现仍来自冻结的内置目录。
# 构建单元携带上下文摘要，缓存不能只按源码路径命中。
# cacheReusable=False 表示输入闭包不完整，即使旧缓存存在也重新分析。
# provider_executor 是冻结运行边界，用于约束提供器读取的输入。
# 生产者返回后先限定单元范围，再去重合并，避免外部依赖混入项目事实。
# 源码摘要单独记录，后续查询可识别分析之后的文件漂移。
# 失败摘要使用安全错误分类，不能把原始环境或凭据持久化。
# 错误级诊断和不完整端点都降低完整性，进程正常退出并不足以证明精确。
# 覆盖回执同时记录执行结果与语义完整性，两者不能互相替代。
# 已计算事实不会因缓存写入失败丢失；该失败仅增加明确诊断。
# 缓存命中保留观察内容，reused 只影响执行状态统计。
# required 模式在汇总失败后阻止发布，不允许悄悄返回自动降级结果。
# 多单元事实按统一 IR 合并，身份冲突由合并器报告。
# 最后将来源处置反映到清单，使覆盖统计与实际运行一致。
# 新语言提供器通过注册表扩展，不向协调器增加语言特定解析逻辑。
def run_compiler_analysis(
    paths: ModusPaths,
    config: ModusConfig,
    *,
    mode: str | None = None,
    provider_executor: Callable[..., SemanticFacts] | None = None,
    extensions: ExtensionRegistry | None = None,
) -> CompilerSemanticResult:
    from modus.machine.facts.extension_registry import builtin_extensions

    registry = extensions or builtin_extensions()
    selected_mode = str(mode or config.init_analysis.compiler.mode)
    if selected_mode not in {"auto", "required", "off"}:
        raise ConfigurationError("machine.facts.invalid-compiler-mode")
    # Off is a real zero-tool path. Discovery itself probes toolchains and may
    # resolve build metadata, so it belongs strictly after this branch.
    if selected_mode == "off":
        context = BuildContext((), (), (), build_source_inventory(paths.root))
        result = CompilerSemanticResult("off", SemanticFacts(), context, (), (), ())
        return result
    context = BuildContextResolver(paths, config).resolve()
    from modus.knowledge_reader import BoundKnowledgeReadError
    from modus.machine.corpus.input_snapshot import read_bytes

    source_digests = {}
    for unit in context.units:
        for source in unit.files:
            try:
                raw = read_bytes(paths.root, source)
                source_digests[source.relative_to(paths.root).as_posix()] = hashlib.sha256(
                    raw
                ).hexdigest()
            except (BoundKnowledgeReadError, OSError, ValueError):
                continue
    supported_files = int(context.inventory.coverage()["supportedFiles"])
    if supported_files and not context.units and selected_mode == "required":
        raise CompilerSemanticError("machine.facts.zero-build-units")
    facts: list[SemanticFacts] = []
    status: list[dict[str, Any]] = []
    reused: list[str] = []
    analyzed: list[str] = []
    failures: list[str] = []
    tools = {item.language: item for item in context.toolchains}
    for unit in context.units:
        cache_reusable = unit.metadata.get("cacheReusable", True)
        cached = (
            _load_facts(_facts_path(paths, unit), context_digest=unit.context_digest)
            if cache_reusable
            else None
        )
        state, reason = "analyzed", "machine.facts.analysis-complete"
        unit_facts = SemanticFacts()
        if cached is not None:
            unit_facts = cached
            state, reason = "reused", "machine.facts.cache-hit"
            reused.append(unit.id)
        elif not tools[unit.language].available:
            state, reason = "failed", "machine.facts.toolchain-unavailable"
            failures.append("machine.facts.toolchain-unavailable")
        else:
            try:
                provider = registry.compiler_providers[unit.language]
                runner = provider.implementation
                raw_facts = (
                    provider_executor(runner, paths, config, unit)
                    if provider_executor
                    else runner(paths, config, unit)
                )
                unit_facts = _merge_facts((scope_facts(raw_facts, unit),))
                analyzed.append(unit.id)
            except (CompilerSemanticError, OSError, subprocess.SubprocessError, ValueError) as exc:
                state, reason = "failed", "machine.facts.execution-failed"
                failures.append(safe_error_summary(exc))
        incomplete = [
            item.code
            for item in unit_facts.diagnostics
            if item.severity == "error"
            or item.code.endswith(
                ("parse-failed", "translation-unit-failed", "identity-conflict", "missing-endpoint")
            )
        ]
        if incomplete:
            reason = "machine.facts.analysis-partial"
            failures.extend(sorted(set(incomplete)))
        unit_facts = receipt_facts(
            scope_facts(unit_facts, unit), unit, paths.root, status=state, reason=reason
        )
        if state == "analyzed" and cache_reusable:
            # A persistence failure cannot discard already-computed observations.
            try:
                _write_facts(_facts_path(paths, unit), unit, unit_facts)
            except (OSError, ValueError) as exc:
                unit_facts = replace(
                    unit_facts,
                    diagnostics=(
                        *unit_facts.diagnostics,
                        SemanticDiagnostic(
                            "machine.facts.cache-write-failed",
                            safe_error_summary(exc),
                            "warning",
                            "compiler-cache",
                            unit.id,
                        ),
                    ),
                )
        facts.append(unit_facts)
        status.append(
            {
                "buildUnitId": unit.id,
                "contextId": unit.analysis_context_id,
                "provider": registry.compiler_providers[unit.language].version,
                "status": state,
                "reasonCode": reason,
                "cacheReusable": bool(cache_reusable),
                "inputCompleteness": unit.metadata.get("inputCoverage", "complete"),
                "analysisCompleteness": "unavailable"
                if state == "failed"
                else "partial"
                if incomplete
                else "complete",
            }
        )
    if failures and selected_mode == "required":
        raise ProviderError(
            "machine.facts.required-provider-failed",
            "required compiler semantics are unavailable",
        )
    diagnostics = (
        *context.diagnostics,
        *(
            SemanticDiagnostic("machine.facts.fallback", message, "warning", "compiler-semantics")
            for message in failures
        ),
    )
    merged = _merge_facts((*facts, SemanticFacts(diagnostics=diagnostics)))
    context = _apply_source_dispositions(context, status, (merged,))
    result = CompilerSemanticResult(
        selected_mode,
        merged,
        context,
        tuple(status),
        tuple(reused),
        tuple(analyzed),
        source_digests=source_digests,
    )
    return result


__all__ = [
    "CompilerSemanticError",
    "CompilerSemanticResult",
    "PROVIDER_VERSIONS",
    "run_compiler_analysis",
]
