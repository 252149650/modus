"""Coordinate safe, reproducible build-unit discovery across supported languages."""

from __future__ import annotations

from dataclasses import replace

from modus.config import ModusConfig
from modus.machine.corpus.source_inventory import (
    build_source_inventory,
)
from modus.machine.facts.build_contracts import (
    BuildContext,
    BuildUnit,
    Toolchain,
)
from modus.machine.facts.build_resolvers import (
    CppResolverMixin,
    GoResolverMixin,
    JavaResolverMixin,
    PythonResolverMixin,
    TypeScriptResolverMixin,
)
from modus.machine.facts.build_support import (
    detect_toolchains,
)
from modus.machine.facts.semantic_ir import SemanticDiagnostic
from modus.machine.languages import PROVIDER_VERSIONS
from modus.machine.runtime.toolchains import java_executable
from modus.paths import ModusPaths


# 协调器负责跨语言组装，各 mixin 只发现本语言构建单元。
# 源码清单先统一冻结，单元发现不能各自扩大扫描范围。
# 工具链描述是已观察能力，不是自动下载安装依赖的授权。
# materialize 标志交给语言实现解释，公共入口保持同一上下文形状。
class BuildContextResolver(
    TypeScriptResolverMixin,
    PythonResolverMixin,
    JavaResolverMixin,
    GoResolverMixin,
    CppResolverMixin,
):
    """Discover compiler units without running project lifecycle or network work."""

    # 记录探测时的可执行文件与版本，让封存阶段能发现工具链漂移。
    # 内部按语言索引工具链，外部仍保留有序不可变描述序列。
    # 清单包含不支持的源码，后续可明确说明结构回退覆盖范围。
    def __init__(
        self,
        paths: ModusPaths,
        config: ModusConfig,
        *,
        materialize: bool = True,
    ) -> None:
        self.paths = paths
        self.config = config
        self.materialize = materialize
        self.toolchains = detect_toolchains()
        from modus.machine.corpus.input_snapshot import active_snapshot

        snapshot = active_snapshot()
        if snapshot is not None:
            for tool in self.toolchains:
                name, args = {
                    "python": ("python", ("--version",)),
                    "java": ("javac", ("-version",)),
                    "go": ("go", ("version",)),
                    "cpp": ("clang++", ("--version",)),
                }[tool.language]
                snapshot.record_toolchain(
                    name=name, executable=tool.executable, args=args, observed_version=tool.version
                )
        self._tools = {item.language: item for item in self.toolchains}
        self.inventory = build_source_inventory(self.paths.root)

    # 每种语言独立贡献单元和诊断，再以稳定键排序组装。
    # 未闭合的 SDK/Maven 输入会禁用持久缓存复用，而非假称已冻结全部依赖。
    # cacheReusable 与 inputCoverage 一起说明复用限制，不改变已得到的源码观察。
    # 源码清单有支持文件却无构建单元时需独立诊断，不能当作空项目成功。
    # 不支持精确提供器的语言仍可拥有结构证据，因此报告为降级而非删除。
    # 返回值保留清单，使调用者能核对单元选择与全项目覆盖。
    # 跨语言阶段不在此处绑定符号，解析关系属于后续 resolution 层。
    def resolve(self) -> BuildContext:
        units: list[BuildUnit] = []
        diagnostics: list[SemanticDiagnostic] = []
        units.extend(self._python_units(diagnostics))
        units.extend(self._typescript_units(diagnostics))
        units.extend(self._java_units(diagnostics))
        units.extend(self._go_units(diagnostics))
        units.extend(self._cpp_units(diagnostics))
        from modus.machine.corpus.input_snapshot import active_snapshot

        snapshot = active_snapshot()
        for index, unit in enumerate(units):
            gap = ""
            detail = ""
            if unit.language == "cpp":
                gap = "machine.facts.cpp-external-inputs-unclosed"
                detail = (
                    "Project quoted headers are observed; SDK, angle and macro includes are "
                    "not a closed input set. This unit is reanalyzed instead of using a persistent cache."
                )
            elif unit.language == "java" and unit.build_system == "maven":
                gap = "machine.facts.maven-model-inputs-unclosed"
                detail = (
                    "Project POM bytes and resolved classpath binaries are frozen; Maven local "
                    "repository metadata, settings and model activation are managed by the local tool."
                )
            if gap != "":
                units[index] = replace(
                    unit,
                    metadata={**unit.metadata, "cacheReusable": False, "inputCoverage": "partial"},
                )
                if snapshot is not None:
                    snapshot.record_input_gap(gap, detail, context_id=unit.analysis_context_id)
        coverage = self.inventory.coverage()
        for language, counts in sorted(coverage["byLanguage"].items()):
            unsupported = int(counts.get("unsupported", 0))
            if unsupported:
                diagnostics.append(
                    SemanticDiagnostic(
                        code="machine.facts.language-unsupported",
                        message=(
                            f"{unsupported} {language} source file(s) have no precise "
                            "S2A Provider and remain structural-only"
                        ),
                        severity="warning",
                        provider="build-context",
                    )
                )
        supported_files = int(coverage["supportedFiles"])
        if supported_files and not units:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.zero-build-units",
                    message=(
                        f"inventory found {supported_files} supported source files but no "
                        "compiler build units were resolved"
                    ),
                    severity="warning",
                    provider="build-context",
                )
            )
        return BuildContext(
            units=tuple(sorted(units, key=lambda item: (item.language, item.id))),
            toolchains=self.toolchains,
            diagnostics=tuple(diagnostics),
            inventory=self.inventory,
        )


__all__ = [
    "BuildContext",
    "BuildContextResolver",
    "BuildUnit",
    "PROVIDER_VERSIONS",
    "Toolchain",
    "java_executable",
    "detect_toolchains",
]
