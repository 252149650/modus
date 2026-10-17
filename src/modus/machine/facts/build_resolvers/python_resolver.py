"""Discover Python build units and their optional semantic enhancement.

Configured SCIP input takes precedence over local discovery. The built-in
Python provider remains the baseline when optional enhancement is unavailable;
diagnostics preserve the reason for that degradation.
"""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from modus.config import ModusConfig
from modus.machine.corpus.input_snapshot import input_exists
from modus.machine.corpus.source_inventory import (
    SourceInventory,
)
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.build_contracts import repository_relative as _relative
from modus.machine.facts.build_support import (
    _digest_unit,
)
from modus.machine.facts.semantic_ir import SemanticDiagnostic
from modus.paths import ModusPaths

from .base import ResolverContext


class PythonResolverMixin(ResolverContext):
    """Resolve one language without owning cross-language orchestration."""

    paths: ModusPaths
    config: ModusConfig
    inventory: SourceInventory
    materialize: bool

    # 构建描述和选定提供器共同进入上下文摘要，依赖变化会使旧证据失效。
    # 显式 SCIP 文件也作为描述输入记录，不能只记录其路径。
    def _python_units(self, diagnostics: list[SemanticDiagnostic]) -> list[BuildUnit]:
        files = self.inventory.files_for(self.paths.root, "python")
        if not files:
            return []
        settings = asdict(self.config.init_analysis.compiler.python)
        provider, index_path, scip_executable = self._python_provider(settings, diagnostics)
        descriptors = tuple(
            path
            for path in (
                self.paths.root / "pyproject.toml",
                self.paths.root / "setup.py",
                self.paths.root / "setup.cfg",
                self.paths.root / "requirements.txt",
                self.paths.root / "uv.lock",
                self.paths.root / "poetry.lock",
            )
            if input_exists(self.paths.root, path)
        )
        if index_path is not None:
            descriptors = (*descriptors, index_path)
        root = self.paths.root
        digest = _digest_unit(
            self.paths.root,
            language="python",
            build_system="python-project" if descriptors else "python-source",
            files=files,
            descriptors=descriptors,
            settings={**settings, "resolved_provider": provider},
            toolchain=self._tools["python"],
        )
        return [
            BuildUnit(
                id=f"python:{_relative(self.paths.root, root)}",
                language="python",
                root=root,
                files=files,
                context_digest=digest,
                build_system="python-project" if descriptors else "python-source",
                metadata={
                    "provider": provider,
                    "scipIndex": _relative(self.paths.root, index_path) if index_path else "",
                    "scipExecutable": scip_executable if provider == "scip-local" else "",
                    "descriptors": [_relative(self.paths.root, item) for item in descriptors],
                },
            )
        ]

    # 仅发现已有工具，不安装或执行 setup.py；显式坏索引不触发替代生成。
    def _python_provider(
        self, settings: Mapping[str, Any], diagnostics: list[SemanticDiagnostic]
    ) -> tuple[str, Path | None, str | None]:
        """Select configured evidence before considering optional local tooling.

        An invalid explicit index remains a diagnostic and uses the builtin
        provider; it must not silently switch to generating a different index.
        """
        configured_index = str(settings["scip_index"] or "")
        provider = "builtin"
        index_path: Path | None = None
        scip_executable = shutil.which("scip-python")
        if configured_index:
            candidate = (self.paths.root / configured_index).resolve()
            try:
                candidate.relative_to(self.paths.root)
            except ValueError:
                diagnostics.append(
                    SemanticDiagnostic(
                        code="machine.facts.scip-index-outside-root",
                        message="configured Python SCIP index must stay inside the repository",
                        severity="error",
                        provider="python-builtin",
                    )
                )
            else:
                if input_exists(self.paths.root, candidate):
                    index_path = candidate
                    provider = "scip"
                else:
                    diagnostics.append(
                        SemanticDiagnostic(
                            code="machine.facts.scip-index-unavailable",
                            message="configured Python SCIP index does not exist; builtin provider will be used",
                            severity="warning",
                            provider="python-builtin",
                        )
                    )
        elif (
            scip_executable
            and importlib.util.find_spec("scip_pb2") is not None
            and settings["provider"] in {"auto", "scip"}
        ):
            provider = "scip-local"
        elif settings["provider"] == "scip":
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.scip-index-required",
                    message="python.provider=scip requires a repository-local pre-generated scip_index",
                    severity="error",
                    provider="python-builtin",
                )
            )
        return provider, index_path, scip_executable


__all__ = ["PythonResolverMixin"]
