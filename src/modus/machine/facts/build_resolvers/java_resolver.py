"""Discover Java source groups and their declared classpath context.

Descriptor selection, optional classpath materialization and diagnostics have
separate helpers. Classpath fingerprinting also records snapshot dependencies,
so discovery order is part of the captured input contract.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from modus.artifacts_io import atomic_write_text
from modus.config import ModusConfig
from modus.machine.corpus.input_snapshot import input_exists, read_bytes
from modus.machine.corpus.source_inventory import (
    SourceInventory,
)
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.build_contracts import repository_relative as _relative
from modus.machine.facts.build_support import (
    _MAVEN_DEPENDENCY_PLUGIN_VERSIONS,
    _classpath_fingerprint,
    _descriptors,
    _digest_unit,
    _input_text,
    _java_class_directories,
    _maven_query_is_safe,
)
from modus.machine.facts.semantic_ir import SemanticDiagnostic, canonical_json
from modus.paths import ModusPaths

from .base import ResolverContext


class JavaResolverMixin(ResolverContext):
    """Resolve one language without owning cross-language orchestration."""

    paths: ModusPaths
    config: ModusConfig
    inventory: SourceInventory
    materialize: bool

    # 只运行固定的离线依赖查询目标，不运行项目构建生命周期。
    # 查询前检查 Maven 配置，并移除可注入 JVM 或 Maven 参数的环境变量。
    # 子进程采用参数数组与超时，失败保留诊断并继续使用已知类路径。
    # 类路径输出另设大小限制，成功退出也不能引入无界读取。
    def _maven_classpath(
        self,
        poms: tuple[Path, ...],
        diagnostics: list[SemanticDiagnostic],
    ) -> tuple[str, str]:
        """Resolve dependency paths through a fixed, offline, non-lifecycle goal."""
        safe, reason = _maven_query_is_safe(self.paths.root, poms)
        if not safe:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.maven-query-blocked",
                    message=f"Maven classpath query was blocked: {reason}",
                    severity="warning",
                    provider="java-javac",
                )
            )
            return "", ""
        executable = shutil.which("mvn")
        if not executable:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.maven-unavailable",
                    message="mvn is unavailable; Java analysis will use configured/build output classpath only",
                    severity="warning",
                    provider="java-javac",
                )
            )
            return "", ""

        source_poms = tuple(pom for pom in poms if (pom.parent / "src" / "main" / "java").is_dir())
        if not source_poms:
            source_poms = poms[:1]
        descriptor_hash = hashlib.sha256()
        for pom in poms:
            descriptor_hash.update(_relative(self.paths.root, pom).encode("utf-8"))
            try:
                descriptor_hash.update(read_bytes(self.paths.root, pom))
            except (OSError, ValueError):
                pass
        cache_key = descriptor_hash.hexdigest()[:24]
        combined = self.paths.compiler_cache_dir / "build-context" / f"maven-{cache_key}.classpath"
        # A POM-only key cannot prove freshness of local repository metadata or
        # Maven settings. Re-run the bounded offline query; the compiler cache
        # independently validates the actual resolved binary input content.
        from modus.machine.corpus.input_snapshot import active_snapshot

        snapshot = active_snapshot()
        query_root = snapshot.mirror_root() if snapshot is not None else self.paths.root

        output_dir = self.paths.compiler_cache_dir / "build-context" / f"maven-{cache_key}"
        output_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        for name in (
            "MAVEN_ARGS",
            "MAVEN_OPTS",
            "JAVA_TOOL_OPTIONS",
            "_JAVA_OPTIONS",
            "JDK_JAVA_OPTIONS",
        ):
            env.pop(name, None)
        entries: list[str] = []
        selected_version = ""
        failures: list[str] = []
        for position, pom in enumerate(source_poms):
            output = output_dir / f"unit-{position}.classpath"
            output.unlink(missing_ok=True)
            versions = (
                (selected_version,) if selected_version else _MAVEN_DEPENDENCY_PLUGIN_VERSIONS
            )
            completed: subprocess.CompletedProcess[str] | None = None
            for version in versions:
                try:
                    # NOCA:dangerous-subprocess-use-audit(已检查 Maven 配置，固定离线插件目标，路径仅作为独立参数传递)
                    completed = subprocess.run(
                        [
                            executable,
                            "--offline",
                            "--batch-mode",
                            "--no-transfer-progress",
                            "-Dstyle.color=never",
                            "-f",
                            str(query_root / pom.relative_to(self.paths.root)),
                            "-DincludeScope=test",
                            f"-Dmdep.outputFile={output}",
                            f"-Dmdep.pathSeparator={os.pathsep}",
                            f"org.apache.maven.plugins:maven-dependency-plugin:{version}:build-classpath",
                        ],
                        shell=False,
                        cwd=query_root,
                        env=env,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    failures.append(f"{_relative(self.paths.root, pom)}: {exc}")
                    completed = None
                    break
                if completed.returncode == 0 and output.is_file():
                    selected_version = version
                    break
            if completed is None or completed.returncode != 0 or not output.is_file():
                detail = ""
                if completed is not None:
                    detail_lines = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
                    detail = detail_lines[0][:500] if detail_lines else "query failed"
                failures.append(f"{_relative(self.paths.root, pom)}: {detail or 'query failed'}")
                continue
            if output.stat().st_size > 8 * 1024 * 1024:
                failures.append(f"{_relative(self.paths.root, pom)}: classpath exceeds 8 MiB")
                continue
            entries.extend(
                item.replace(str(query_root), str(self.paths.root))
                for item in output.read_text(encoding="utf-8").strip().split(os.pathsep)
                if item
            )
        entries.extend(str(item.resolve()) for item in _java_class_directories(self.paths.root))
        value = os.pathsep.join(dict.fromkeys(entries))
        if value:
            atomic_write_text(combined, value + "\n")
        if failures:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.maven-classpath-partial",
                    message="; ".join(failures[:8]),
                    severity="warning",
                    provider="java-javac",
                )
            )
        return value, _classpath_fingerprint(value) if value else ""

    # 构建单元同时绑定源码、描述文件和类路径摘要，依赖变化会使语义缓存失效。
    def _java_units(self, diagnostics: list[SemanticDiagnostic]) -> list[BuildUnit]:
        files = self.inventory.files_for(self.paths.root, "java")
        if not files:
            return []
        settings = asdict(self.config.init_analysis.compiler.java)
        build_system, poms, gradle, descriptors = self._java_descriptors(settings, diagnostics)
        classpath_file, classpath_digest, resolved_classpath = self._java_classpath(
            settings, build_system, poms, diagnostics
        )
        settings["resolved_classpath_digest"] = classpath_digest
        from modus.machine.corpus.input_snapshot import content_digest

        # Javac's sourcepath is the captured repository root. Its implicit
        # source lookup can therefore observe another source set: retain the
        # whole Java sourcepath set as an action input until the provider emits
        # a precise successful/negative lookup receipt for that lookup.
        settings["sourcepath_digest"] = hashlib.sha256(
            canonical_json(
                {
                    _relative(self.paths.root, path): content_digest(self.paths.root, path)
                    for path in files
                }
            ).encode()
        ).hexdigest()
        tool = self._tools["java"]
        groups = self._java_source_groups(files, build_system, poms, gradle, descriptors)
        result: list[BuildUnit] = []
        for module_root, source_set, scoped, module_descriptors in groups:
            unit_settings = {**settings, "source_set": source_set}
            digest = _digest_unit(
                self.paths.root,
                language="java",
                build_system=build_system,
                files=scoped,
                descriptors=module_descriptors,
                settings=unit_settings,
                toolchain=tool,
            )
            relative_root = _relative(self.paths.root, module_root)
            base_id = f"java:{relative_root}"
            unit_id = base_id if len(groups) == 1 else f"{base_id}:{source_set}"
            result.append(
                BuildUnit(
                    id=unit_id,
                    language="java",
                    root=module_root,
                    files=scoped,
                    context_digest=digest,
                    build_system=build_system,
                    metadata={
                        "descriptors": [
                            _relative(self.paths.root, item) for item in module_descriptors
                        ],
                        "classpathFile": classpath_file,
                        "resolvedClasspath": resolved_classpath,
                        "sourceSet": source_set,
                    },
                )
            )
        return result

    # 构建描述用于识别上下文，发现描述文件不等于授权执行构建脚本。
    def _java_descriptors(
        self, settings: Mapping[str, Any], diagnostics: list[SemanticDiagnostic]
    ) -> tuple[str, tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
        """Select the build system and contain any explicitly configured model.

        Explicit configuration overrides descriptor discovery. Missing or escaped
        model files produce diagnostics without broadening repository authority.
        """
        requested = settings["build_system"]
        poms = _descriptors(self.paths.root, frozenset({"pom.xml"}))
        gradle = _descriptors(
            self.paths.root,
            frozenset(
                {
                    "build.gradle",
                    "build.gradle.kts",
                    "settings.gradle",
                    "settings.gradle.kts",
                }
            ),
        )
        build_system = "maven" if poms else "gradle" if gradle else "none"
        if requested != "auto":
            build_system = requested
        descriptors = (
            poms if build_system == "maven" else gradle if build_system == "gradle" else ()
        )
        configured_model = str(settings["build_model_file"] or "")
        if configured_model:
            model_path = (self.paths.root / configured_model).resolve()
            try:
                model_path.relative_to(self.paths.root)
            except ValueError:
                diagnostics.append(
                    SemanticDiagnostic(
                        code="machine.facts.java-model-outside-root",
                        message="configured Java build_model_file must stay inside the repository",
                        severity="error",
                        provider="java-javac",
                    )
                )
            else:
                if input_exists(self.paths.root, model_path):
                    descriptors = (*descriptors, model_path)
                else:
                    diagnostics.append(
                        SemanticDiagnostic(
                            code="machine.facts.java-model-unavailable",
                            message="configured Java build_model_file does not exist",
                            severity="error",
                            provider="java-javac",
                        )
                    )
        return build_system, poms, gradle, descriptors

    # 显式类路径在读取边界规范化，路径列表与内容指纹分别保留。
    def _configured_java_classpath(
        self, configured_classpath: str, diagnostics: list[SemanticDiagnostic]
    ) -> tuple[str, str, str]:
        """Return the classpath file, binary digest and resolved path list.

        Resolve only a repository-contained descriptor. Fingerprinting records
        binary inputs in the active snapshot as well as calculating their hash.
        """
        classpath_file = classpath_digest = resolved_classpath = ""
        configured_path = (self.paths.root / configured_classpath).resolve()
        try:
            configured_path.relative_to(self.paths.root)
        except ValueError:
            diagnostics.append(
                SemanticDiagnostic(
                    code="machine.facts.java-classpath-outside-root",
                    message="configured Java classpath_file must stay inside the repository",
                    severity="error",
                    provider="java-javac",
                )
            )
        else:
            if input_exists(self.paths.root, configured_path):
                classpath_file = _relative(self.paths.root, configured_path)
                resolved_classpath = _input_text(self.paths.root, configured_path).strip()
                classpath_digest = _classpath_fingerprint(resolved_classpath)
            else:
                diagnostics.append(
                    SemanticDiagnostic(
                        code="machine.facts.java-classpath-unavailable",
                        message="configured Java classpath_file is missing or exceeds 8 MiB",
                        severity="error",
                        provider="java-javac",
                    )
                )
        return classpath_file, classpath_digest, resolved_classpath

    # 配置、离线查询与现有构建输出按既定策略组合，缺失信息通过诊断暴露。
    def _java_classpath(
        self,
        settings: Mapping[str, Any],
        build_system: str,
        poms: tuple[Path, ...],
        diagnostics: list[SemanticDiagnostic],
    ) -> tuple[str, str, str]:
        """Apply configured, offline Maven or conventional classpath policy.

        Build output directories extend the selected path list in first-seen
        order. Both preliminary and final fingerprints retain input observations.
        """
        configured_classpath = str(settings["classpath_file"] or "")
        classpath_file = ""
        classpath_digest = ""
        resolved_classpath = ""
        if configured_classpath:
            classpath_file, classpath_digest, resolved_classpath = self._configured_java_classpath(
                configured_classpath, diagnostics
            )
        elif build_system == "maven" and self.materialize:
            _classpath, classpath_digest = self._maven_classpath(poms, diagnostics)
            if _classpath:
                resolved_classpath = _classpath
                descriptor_hash = hashlib.sha256()
                for pom in poms:
                    descriptor_hash.update(_relative(self.paths.root, pom).encode("utf-8"))
                    try:
                        descriptor_hash.update(read_bytes(self.paths.root, pom))
                    except (OSError, ValueError):
                        pass
                classpath_file = _relative(
                    self.paths.root,
                    self.paths.compiler_cache_dir
                    / "build-context"
                    / f"maven-{descriptor_hash.hexdigest()[:24]}.classpath",
                )
        else:
            self._java_classpath_diagnostic(settings, build_system, poms, diagnostics)
        classpath_entries = [item for item in resolved_classpath.split(os.pathsep) if item]
        classpath_entries.extend(
            str(item.resolve()) for item in _java_class_directories(self.paths.root)
        )
        resolved_classpath = os.pathsep.join(dict.fromkeys(classpath_entries))
        classpath_digest = _classpath_fingerprint(resolved_classpath)
        return classpath_file, classpath_digest, resolved_classpath

    # 类路径可用性独立报告，使部分解析不会被误认作完整编译器覆盖。
    def _java_classpath_diagnostic(
        self,
        settings: Mapping[str, Any],
        build_system: str,
        poms: tuple[Path, ...],
        diagnostics: list[SemanticDiagnostic],
    ) -> None:
        """Explain a non-materialized build model without executing a lifecycle.

        Maven reports whether its offline query would be permitted. Gradle keeps
        the existing distinction between a model file, authority and conventions.
        """
        if build_system == "maven":
            safe, reason = _maven_query_is_safe(self.paths.root, poms)
            diagnostics.append(
                SemanticDiagnostic(
                    code=(
                        "machine.facts.maven-classpath-query-available"
                        if safe
                        else "machine.facts.maven-query-blocked"
                    ),
                    message=(
                        "an analysis build may run the controlled offline Maven classpath query"
                        if safe
                        else f"Maven classpath query would be blocked: {reason}"
                    ),
                    severity="info" if safe else "warning",
                    provider="java-javac",
                )
            )
        elif build_system == "gradle":
            model_file = str(settings["build_model_file"] or "")
            diagnostics.append(
                SemanticDiagnostic(
                    code=(
                        "machine.facts.gradle-model-file"
                        if model_file
                        else "machine.facts.gradle-model-query-authorized"
                        if settings["allow_gradle_model_query"]
                        else "machine.facts.gradle-conventions-only"
                    ),
                    message=(
                        "using the pre-generated Gradle model as an explicit build input"
                        if model_file
                        else "Gradle configuration-model query is explicitly authorized; no task will be run"
                        if settings["allow_gradle_model_query"]
                        else "Gradle model query is disabled; only conventional main/test source sets are used"
                    ),
                    severity="info"
                    if model_file or settings["allow_gradle_model_query"]
                    else "warning",
                    provider="java-javac",
                )
            )

    # 源码按可识别的单元范围分组，为不同构建上下文保留独立身份。
    def _java_source_groups(
        self,
        files: tuple[Path, ...],
        build_system: str,
        poms: tuple[Path, ...],
        gradle: tuple[Path, ...],
        descriptors: tuple[Path, ...],
    ) -> list[tuple[Path, str, tuple[Path, ...], tuple[Path, ...]]]:
        """Partition conventional main/test roots, retaining unassigned sources.

        Root descriptors are inherited by module source sets. Files outside those
        conventions stay in a repository-level main unit rather than disappearing.
        """
        groups: list[tuple[Path, str, tuple[Path, ...], tuple[Path, ...]]] = []
        module_roots = (
            tuple(path.parent for path in poms)
            if build_system == "maven"
            else tuple(path.parent for path in gradle if path.name.startswith("build.gradle"))
        )
        source_roots = {
            module_root / "src" / source_set / "java": (module_root, source_set)
            for module_root in module_roots or (self.paths.root,)
            for source_set in ("main", "test")
        }
        source_index: dict[Path, list[Path]] = {}
        for path in files:
            for parent in path.parents:
                if parent in source_roots:
                    source_index.setdefault(parent, []).append(path)
        descriptor_index: dict[Path, list[Path]] = {}
        for descriptor in descriptors:
            descriptor_index.setdefault(descriptor.parent, []).append(descriptor)
        for module_root in module_roots or (self.paths.root,):
            for source_set in ("main", "test"):
                source_root = module_root / "src" / source_set / "java"
                scoped = tuple(source_index.get(source_root, ()))
                if scoped:
                    module_descriptors = tuple(
                        sorted(
                            {
                                *descriptor_index.get(module_root, ()),
                                *descriptor_index.get(self.paths.root, ()),
                            }
                        )
                    )
                    groups.append((module_root, source_set, scoped, module_descriptors))
        assigned = {path for _, _, scoped, _ in groups for path in scoped}
        remaining = tuple(path for path in files if path not in assigned)
        if remaining:
            groups.append((self.paths.root, "main", remaining, descriptors))
        return groups


__all__ = ["JavaResolverMixin"]
