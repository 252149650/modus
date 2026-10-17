"""Discover TypeScript units with a locally installed compiler API.

Toolchain probing records both the executable and the resolved package version.
Unit discovery consumes that observation without installing dependencies or
executing project build scripts.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from modus.config import ModusConfig
from modus.machine.corpus.input_snapshot import input_exists
from modus.machine.corpus.source_inventory import (
    SourceInventory,
)
from modus.machine.facts.build_contracts import BuildUnit, Toolchain
from modus.machine.facts.build_contracts import repository_relative as _relative
from modus.machine.facts.build_support import (
    _digest_unit,
    _input_text,
    _version,
)
from modus.machine.facts.semantic_ir import SemanticDiagnostic
from modus.paths import ModusPaths

from .base import ResolverContext


class TypeScriptResolverMixin(ResolverContext):
    """Resolve one language without owning cross-language orchestration."""

    paths: ModusPaths
    config: ModusConfig
    inventory: SourceInventory
    materialize: bool

    # JS 与 TS 文件按最近的 tsconfig 分组，避免跨配置混合编译上下文。
    # 配置和 package 描述一并参与摘要，工具版本则由探测结果提供。
    def _typescript_units(self, diagnostics: list[SemanticDiagnostic]) -> list[BuildUnit]:
        files = tuple(
            sorted(
                set(
                    (
                        *self.inventory.files_for(self.paths.root, "typescript"),
                        *self.inventory.files_for(self.paths.root, "javascript"),
                    )
                )
            )
        )
        if not files:
            return []
        tool, module = self._typescript_toolchain()
        executable = tool.executable
        groups: dict[Path, list[Path]] = {}
        for source in files:
            directory = source.parent
            while directory != self.paths.root and not input_exists(
                self.paths.root, directory / "tsconfig.json"
            ):
                directory = directory.parent
            groups.setdefault(directory, []).append(source)
        units = []
        for directory, members in sorted(groups.items()):
            configuration = directory / "tsconfig.json"
            descriptors = tuple(
                path
                for path in (configuration, directory / "package.json")
                if input_exists(self.paths.root, path)
            )
            metadata = {
                "nodeExecutable": executable,
                "typescriptModule": str(module or ""),
                "tsconfigFile": str(configuration)
                if input_exists(self.paths.root, configuration)
                else "",
            }
            digest = _digest_unit(
                self.paths.root,
                language="typescript",
                build_system="typescript",
                files=tuple(members),
                descriptors=descriptors,
                settings=metadata,
                toolchain=tool,
            )
            units.append(
                BuildUnit(
                    "typescript:" + _relative(self.paths.root, directory),
                    "typescript",
                    directory,
                    tuple(members),
                    digest,
                    "typescript",
                    metadata,
                )
            )
        return units

    # 优先使用项目内 TypeScript，其次检查 Node 安装附近的既有模块。
    # 版本读取失败等同工具不可用，不能把不明模块当成有效编译器。
    def _typescript_toolchain(self) -> tuple[Toolchain, Path | None]:
        """Locate existing Node/TypeScript installations without downloading them.

        Record Node's observed version in the active snapshot before publishing
        the toolchain to the resolver. Package groups all reuse that same result.
        """
        executable = shutil.which("node") or ""
        node_version, reason = _version(executable, "--version")
        module_candidates = [self.paths.root / "node_modules" / "typescript"]
        if executable:
            binary = Path(executable).resolve()
            module_candidates.extend(
                [
                    binary.parent.parent / "lib/node_modules/typescript",
                    binary.parent.parent / "node_modules/typescript",
                ]
            )
        module = next(
            (
                path.resolve()
                for path in module_candidates
                if input_exists(self.paths.root, path / "lib/typescript.js")
            ),
            None,
        )
        version = ""
        if module:
            try:
                version = str(
                    json.loads(_input_text(self.paths.root, module / "package.json"))["version"]
                )
            except (OSError, ValueError, KeyError, TypeError):
                module = None
        tool = Toolchain(
            "typescript",
            executable,
            node_version + "/typescript-" + version,
            bool(executable and node_version and module),
            reason or ("an existing TypeScript installation was not found" if not module else ""),
        )
        from modus.machine.corpus.input_snapshot import active_snapshot

        snapshot = active_snapshot()
        if snapshot is not None:
            snapshot.record_toolchain(
                name="node",
                executable=executable,
                args=("--version",),
                observed_version=node_version,
            )
        self._tools["typescript"] = tool
        self.toolchains = (*self.toolchains, tool)
        return tool, module


__all__ = ["TypeScriptResolverMixin"]
