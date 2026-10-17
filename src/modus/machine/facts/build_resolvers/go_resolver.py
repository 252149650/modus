"""Discover Go units from module descriptors and the captured source inventory.

Package selection remains a compiler concern when an import-path pattern
cannot be interpreted as a repository-relative directory selector.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from modus.config import ModusConfig
from modus.machine.corpus.input_snapshot import input_exists
from modus.machine.corpus.source_inventory import (
    SourceInventory,
)
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.build_contracts import repository_relative as _relative
from modus.machine.facts.build_support import (
    _descriptors,
    _digest_unit,
    _matches_go_patterns,
)
from modus.machine.facts.semantic_ir import SemanticDiagnostic
from modus.paths import ModusPaths

from .base import ResolverContext


class GoResolverMixin(ResolverContext):
    """Resolve one language without owning cross-language orchestration."""

    paths: ModusPaths
    config: ModusConfig
    inventory: SourceInventory
    materialize: bool

    # Go 语义边界以包目录分组，同模块的多个包仍保留独立单元。
    # 最近的 go.mod 确定模块归属，缺少描述时退回源码模式。
    # 仅纳入冻结清单中的文件，配置 patterns 不能扩大到未捕获源码。
    # go.sum 与 go.work 共同影响上下文身份，依赖和工作区变化需失效。
    # configuredPatterns 保留用户意图，实际提供器在已选包内使用点路径。
    # 排序后的包和文件确保身份不依赖文件系统遍历顺序。
    def _go_units(self, diagnostics: list[SemanticDiagnostic]) -> list[BuildUnit]:
        del diagnostics
        files = self.inventory.files_for(self.paths.root, "go")
        if not files:
            return []
        modules = _descriptors(self.paths.root, frozenset({"go.mod"}))
        tool = self._tools["go"]
        settings = asdict(self.config.init_analysis.compiler.go)
        result: list[BuildUnit] = []
        packages: dict[Path, list[Path]] = {}
        for source in files:
            packages.setdefault(source.parent, []).append(source)
        module_roots = {path.parent for path in modules}
        for package_root, package_files in sorted(packages.items()):
            module_root = next(
                (path for path in (package_root, *package_root.parents) if path in module_roots),
                self.paths.root,
            )
            scoped = tuple(
                path
                for path in package_files
                if _matches_go_patterns(module_root, path, list(settings["patterns"]))
            )
            if not scoped:
                continue
            descriptors = tuple(
                item
                for item in (
                    module_root / "go.mod",
                    module_root / "go.sum",
                    self.paths.root / "go.work",
                )
                if input_exists(self.paths.root, item)
            )
            digest = _digest_unit(
                self.paths.root,
                language="go",
                build_system="go-mod"
                if input_exists(self.paths.root, module_root / "go.mod")
                else "go-source",
                files=scoped,
                descriptors=descriptors,
                settings={**settings, "package": _relative(self.paths.root, package_root)},
                toolchain=tool,
            )
            result.append(
                BuildUnit(
                    id=f"go:{_relative(self.paths.root, package_root)}",
                    language="go",
                    root=package_root,
                    files=scoped,
                    context_digest=digest,
                    build_system=(
                        "go-mod"
                        if input_exists(self.paths.root, module_root / "go.mod")
                        else "go-source"
                    ),
                    metadata={
                        "patterns": ["."],
                        "configuredPatterns": list(settings["patterns"]),
                        "buildTags": list(settings["build_tags"]),
                        "cgo": settings["cgo"],
                        "moduleRoot": _relative(self.paths.root, module_root),
                        "goWork": (
                            str((self.paths.root / "go.work").resolve())
                            if input_exists(self.paths.root, self.paths.root / "go.work")
                            else "off"
                        ),
                    },
                )
            )
        return result


__all__ = ["GoResolverMixin"]
