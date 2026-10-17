"""Translate admitted compilation-database actions into reproducible C++ units.

Only inventory-owned source paths become units. Canonical command variants
retain distinct actions for the same source while making their identities
independent of incidental JSON serialization.
"""

from __future__ import annotations

import json
import shlex
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
    _cpp_header_closure,
    _descriptors,
    _digest_unit,
    _input_text,
)
from modus.machine.facts.semantic_ir import SemanticDiagnostic, canonical_json, stable_id
from modus.paths import ModusPaths

from .base import ResolverContext


class CppResolverMixin(ResolverContext):
    """Resolve one language without owning cross-language orchestration."""

    paths: ModusPaths
    config: ModusConfig
    inventory: SourceInventory
    materialize: bool

    # 缺少编译数据库时仍返回 unresolved 源码单元，结构观察可继续。
    # 数据库解析失败形成诊断，不应抹去其他数据库的有效单元。
    def _cpp_units(self, diagnostics: list[SemanticDiagnostic]) -> list[BuildUnit]:
        files = self.inventory.files_for(self.paths.root, "cpp")
        if not files:
            return []
        settings = asdict(self.config.init_analysis.compiler.cpp)
        configured = str(settings["compilation_database"] or "")
        candidates = (
            (self.paths.root / configured,)
            if configured
            else _descriptors(self.paths.root, frozenset({"compile_commands.json"}))
        )
        databases = tuple(
            sorted(path.absolute() for path in candidates if input_exists(self.paths.root, path))
        )
        source_set = set(files)
        if not databases:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.compilation-database-missing",
                    message="compile_commands.json was not found; C++ semantic analysis will fall back",
                    severity="warning",
                    provider="cpp-clang",
                )
            )
        tool = self._tools["cpp"]
        if not databases:
            digest = _digest_unit(
                self.paths.root,
                language="cpp",
                build_system="cpp-source",
                files=files,
                descriptors=(),
                settings=settings,
                toolchain=tool,
            )
            return [
                BuildUnit(
                    id="cpp:unresolved",
                    language="cpp",
                    root=self.paths.root,
                    files=files,
                    context_digest=digest,
                    build_system="cpp-source",
                    metadata={"compilationDatabase": "", "translationUnit": ""},
                )
            ]
        result: list[BuildUnit] = []
        for database in databases:
            try:
                entries = json.loads(_input_text(self.paths.root, database))
            except (OSError, json.JSONDecodeError) as exc:
                diagnostics.append(
                    SemanticDiagnostic(
                        code="machine.facts.cpp-invalid-compilation-database",
                        message=type(exc).__name__,
                        severity="error",
                        provider="cpp-clang",
                    )
                )
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                unit = self._cpp_command_unit(database, entry, source_set, settings)
                if unit is not None:
                    result.append(unit)
        return result

    # 编译数据库可以提及仓库外文件，只有冻结清单中的源码可被接纳。
    # 头文件闭包是编译上下文的一部分，随主文件共同参与内容摘要。
    def _cpp_command_unit(
        self,
        database: Path,
        entry: object,
        source_set: set[Path],
        settings: Mapping[str, Any],
    ) -> BuildUnit | None:
        """Admit one in-repository translation-unit variant from the database.

        The selected source inventory bounds admission. Different commands for
        the same file remain distinct units, including their quoted-header input
        closure and canonical command identity.
        """
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            return None
        directory = Path(str(entry.get("directory") or database.parent)).resolve()
        source = Path(entry["file"])
        source = source.resolve() if source.is_absolute() else (directory / source).resolve()
        try:
            source.relative_to(self.paths.root)
        except ValueError:
            return None
        if source not in source_set:
            return None
        command_entry, variant_id = _cpp_command_variant(self.paths.root, entry, directory, source)
        closure = (
            _cpp_header_closure(self.paths.root, source, entry)
            if settings["dependency_scan"]
            else ()
        )
        unit_files = (source, *closure)
        digest = _digest_unit(
            self.paths.root,
            language="cpp",
            build_system="compilation-database",
            files=unit_files,
            descriptors=(database,),
            settings={
                **settings,
                "translation_unit": _relative(self.paths.root, source),
                "command_entry": command_entry,
                "variant_id": variant_id,
            },
            toolchain=self._tools["cpp"],
        )
        return BuildUnit(
            id=f"cpp:{_relative(self.paths.root, source)}:{variant_id}",
            language="cpp",
            root=directory,
            files=unit_files,
            context_digest=digest,
            build_system="compilation-database",
            metadata={
                "compilationDatabase": str(database),
                "translationUnit": str(source),
                "commandEntry": command_entry,
                "variantId": variant_id,
                "headerClosure": [_relative(self.paths.root, item) for item in closure],
            },
        )


__all__ = ["CppResolverMixin"]


# 这里只拆分命令文本形成参数，不通过 shell 执行数据库中的命令。
# 相同源码的不同编译参数保留不同身份，不能简单按文件去重。
def _cpp_command_variant(
    root: Path, entry: Mapping[str, Any], directory: Path, source: Path
) -> tuple[dict[str, Any], str]:
    """Canonicalize one compile action without executing its command string."""
    # One compile command is one variant, even for the same source.
    command_entry = dict(entry)
    command_entry["directory"] = str(directory)
    command_entry["file"] = str(source)
    arguments = command_entry.get("arguments")
    if not isinstance(arguments, list):
        arguments = shlex.split(str(command_entry.get("command") or ""))
    command_entry["arguments"] = arguments
    command_entry.pop("command", None)
    variant_id = stable_id(
        "variant",
        canonical_json(
            {
                "directory": _relative(root, directory),
                "file": _relative(root, source),
                "arguments": arguments,
                "output": command_entry.get("output", ""),
            }
        ),
    )
    return command_entry, variant_id
