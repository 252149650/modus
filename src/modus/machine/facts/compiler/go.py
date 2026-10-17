"""Local Go package requests and compiler facts."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from modus.artifacts_io import atomic_write_text
from modus.config import ModusConfig
from modus.machine.facts.build_contracts import BuildUnit
from modus.machine.facts.semantic_ir import SemanticFacts, parse_jsonl
from modus.paths import ModusPaths

from .models import CompilerSemanticError
from .process import _asset, _compile_asset, _go_env, _run_process, _safe_name


# 先生成离线包闭包请求，再运行随包 go/types 分析器。
# 提供器输出通过共同 JSONL 契约读取，空声明视为分析不可用。
# 静态或具体方法调用可精确，接口分派仍保留候选边。
# 工作区与 cgo 策略由构建单元和显式配置传入，不继承任意工具覆盖。
def _run_go(paths: ModusPaths, config: ModusConfig, unit: BuildUnit) -> SemanticFacts:
    """Compile the bundled go/types analyzer once, then run it against *unit*'s import closure."""
    executable = shutil.which("go")
    if not executable:
        raise CompilerSemanticError("go is unavailable")
    helper = _compile_asset(
        paths,
        language="go",
        source=_asset("modus_go_analyzer.go"),
        compiler=executable,
    )
    go_work = str(unit.metadata.get("goWork") or "off")
    env = _go_env(
        go_work=go_work,
        cgo=config.init_analysis.compiler.go.cgo,
    )
    request = _go_package_request(paths, unit, executable=executable, env=env)
    completed = _run_process(
        [
            str(helper),
            str(unit.root),
            unit.id,
            unit.context_digest,
            str(request),
        ],
        cwd=unit.root,
        env=env,
        timeout=1200,
    )
    if completed.returncode != 0:
        raise CompilerSemanticError("Go provider failed: " + completed.stderr.strip()[-4000:])
    facts = parse_jsonl(completed.stdout.splitlines())
    if not facts.nodes:
        raise CompilerSemanticError("Go provider emitted no declarations")
    return replace(
        facts,
        edges=tuple(
            replace(
                edge,
                binding_kind=(
                    "runtime-exact"
                    if edge.relation == "calls"
                    and edge.resolution == "exact"
                    and edge.dispatch in {"static", "method"}
                    else "runtime-candidate"
                    if edge.relation == "calls" and edge.dispatch == "interface"
                    else edge.binding_kind
                ),
            )
            for edge in facts.edges
        ),
    )


# go list 输出多个相邻 JSON 对象，不是一个数组或逐行 JSON。
# 解码器返回偏移后继续读取，字符串内部换行不会破坏记录边界。
def _decode_json_stream(value: str) -> list[Mapping[str, Any]]:
    """Decode consecutive whitespace-separated JSON objects (as `go list -json` emits)."""
    decoder = json.JSONDecoder()
    offset = 0
    result: list[Mapping[str, Any]] = []
    while offset < len(value):
        while offset < len(value) and value[offset].isspace():
            offset += 1
        if offset >= len(value):
            break
        item, offset = decoder.raw_decode(value, offset)
        if isinstance(item, Mapping):
            result.append(item)
    return result


# go list 使用只读模块模式，依赖解析不能改写项目描述文件。
# 标准库使用已有导出数据，避免再次解析整个 GOROOT。
# 包中的文件名需为单一名称，不能借相对路径逃出包目录。
# 必须至少包含一个目标项目包，否则成功解析依赖也不算完成目标分析。
# 请求文件原子写入并稳定排序，提供器读取不到半份 JSON。
def _go_package_request(
    paths: ModusPaths,
    unit: BuildUnit,
    *,
    executable: str,
    env: Mapping[str, str],
) -> Path:
    """Freeze the offline import closure consumed by the Go type checker."""
    patterns = [str(item) for item in unit.metadata.get("patterns", ())] or ["./..."]
    tags = [str(item) for item in unit.metadata.get("buildTags", ())]
    argv = [executable, "list", "-deps", "-json", "-e", "-mod=readonly"]
    if tags:
        argv.append("-tags=" + ",".join(tags))
    argv.extend(patterns)
    completed = _run_process(argv, cwd=unit.root, env=env, timeout=600)
    try:
        packages = _decode_json_stream(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CompilerSemanticError(f"go list emitted invalid package JSON: {exc}") from exc
    if not packages:
        detail = (completed.stderr or "go list emitted no packages").strip()[-4000:]
        raise CompilerSemanticError("could not resolve Go import closure: " + detail)

    project_files = {item.resolve() for item in unit.files}
    request: list[dict[str, Any]] = []
    for package in packages:
        import_path = str(package.get("ImportPath") or "")
        directory_text = str(package.get("Dir") or "")
        if not import_path or not directory_text or package.get("Standard") is True:
            # Standard-library export data is loaded by go/importer. Parsing all
            # GOROOT sources would make the provider unnecessarily expensive.
            continue
        directory = Path(directory_text).resolve()
        source_names = [
            *package.get("GoFiles", ()),
            *package.get("CgoFiles", ()),
        ]
        files: list[str] = []
        package_is_project = False
        for name in source_names:
            if not isinstance(name, str) or Path(name).name != name:
                continue
            source = (directory / name).resolve()
            try:
                source.relative_to(directory)
            except ValueError:
                continue
            if not source.is_file():
                continue
            files.append(str(source))
            package_is_project = package_is_project or source in project_files
        if files:
            request.append(
                {
                    "importPath": import_path,
                    "directory": str(directory),
                    "files": sorted(set(files)),
                    "project": package_is_project,
                }
            )
    if not any(item["project"] for item in request):
        raise CompilerSemanticError("go list import closure contains no target project package")
    target = paths.compiler_cache_dir / "requests" / f"{_safe_name(unit.id)}.packages.json"
    atomic_write_text(
        target,
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return target
