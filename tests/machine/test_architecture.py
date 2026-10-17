"""Machine dependency direction, source ownership, and extension contracts."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from modus.machine.errors import ConfigurationError, validate_code
from modus.machine.facts.extension_registry import (
    ExtensionRegistry,
    RegisteredImplementation,
    builtin_extensions,
)
from modus.machine.languages import (
    CapabilityTier,
    capability_for,
    capability_registry,
    validate_language_registry,
)

MACHINE_ROOT = Path(__file__).parents[2] / "src" / "modus" / "machine"


LAYERS = ("corpus", "facts", "resolution", "capability", "projection", "packet")


LAYER_RANK = {name: rank for rank, name in enumerate(LAYERS)}


# 相对导入以所在包解析，普通模块和包入口都要先去掉文件名。
def _module_parts(path: Path) -> list[str]:
    relative = path.relative_to(MACHINE_ROOT).with_suffix("")
    parts = ["modus", "machine", *relative.parts]
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts.pop()
    return parts


# 静态遍历 AST 不导入被检查模块，避免架构检查执行可选工具链。
# 相对层级还原为绝对模块名后才能比较依赖方向。
def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    current_package = _module_parts(path)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(current_package) - node.level + 1
                base = current_package[:keep]
                module = ".".join([*base, *(node.module or "").split(".")])
            else:
                module = node.module or ""
            imported.append(module)
    return imported


# 只给语义流水线分配层序，store/runtime 使用独立横切约束。
# 不属于本包的第三方依赖不参与语义层排序检查。
def _semantic_layer(module: str) -> str | None:
    parts = module.split(".")
    if parts[:2] != ["modus", "machine"] or len(parts) < 3:
        return None
    return parts[2] if parts[2] in LAYER_RANK else None


# 层序约束防止后层知识反向影响原始事实观察。
# 收集全部违规再报错，使一次模块移动的所有依赖问题可同时审查。
def test_semantic_layers_only_depend_on_same_or_earlier_layers() -> None:
    violations: list[str] = []
    for layer, rank in LAYER_RANK.items():
        for path in sorted((MACHINE_ROOT / layer).rglob("*.py")):
            for module in _imported_modules(path):
                target = _semantic_layer(module)
                if target is not None and LAYER_RANK[target] > rank:
                    violations.append(
                        f"{path.relative_to(MACHINE_ROOT)} imports later layer {module}"
                    )
    assert violations == []


# 存储与执行平面应只承载机制，不了解语义阶段的具体实现。
# 这保证各阶段可独立扩展，而公共机制不形成循环依赖。
def test_cross_cutting_planes_do_not_depend_on_semantic_layers() -> None:
    violations: list[str] = []
    for plane in ("store", "runtime"):
        for path in sorted((MACHINE_ROOT / plane).rglob("*.py")):
            for module in _imported_modules(path):
                if _semantic_layer(module) is not None:
                    violations.append(
                        f"{path.relative_to(MACHINE_ROOT)} imports semantic layer {module}"
                    )
    assert violations == []


# 后层读取冻结契约，重新读源码会把不同时间点的证据混在一起。
# 同时检查导入和直接调用，覆盖两种常见的所有权泄漏方式。
def test_projection_and_packet_do_not_reobserve_source() -> None:
    """N5/N6 consume contracts; source observation belongs exclusively to N2."""
    forbidden_modules = {
        "modus.machine.corpus.input_snapshot",
        "modus.machine.corpus.source_snapshot",
        "modus.machine.facts",
    }
    forbidden_names = {
        "active_snapshot",
        "read_bytes",
        "read_text",
        "observe_source_evidence",
        "augment_entries",
        "parser_for",
    }
    violations: list[str] = []
    for layer in ("projection", "packet"):
        for path in sorted((MACHINE_ROOT / layer).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(path):
                if module in forbidden_modules or module.startswith("modus.machine.facts."):
                    violations.append(f"{path.relative_to(MACHINE_ROOT)} imports {module}")
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in forbidden_names
                ):
                    violations.append(f"{path.relative_to(MACHINE_ROOT)} calls {node.func.id}")
    assert violations == []


# 稳定诊断码从源码字面量审计，错误位置直接指向定义处。
# 前缀常量可用于组合，因此排除以点结尾的未完成片段。
def test_literal_machine_codes_are_canonical() -> None:
    violations: list[str] = []
    for path in sorted(MACHINE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("machine.")
                and not node.value.endswith(".")
            ):
                continue
            try:
                validate_code(node.value)
            except ValueError:
                violations.append(f"{path.relative_to(MACHINE_ROOT)}:{node.lineno}:{node.value}")
    assert violations == []


# 语言、提取器、能力等级和编译提供器共同描述支持边界。
# 未知扩展使用 generic 降级，不能宣称具有未实现的语义能力。
# 版本化提供器标识使缓存和维护者能识别扩展契约变化。
def test_language_registry_exposes_auditable_capability_coordinates() -> None:
    document = json.loads((MACHINE_ROOT / "language-registry.json").read_text(encoding="utf-8"))
    assert document["schemaVersion"] == 1
    assert set(document["compilerProviders"]) == {
        "cpp",
        "go",
        "java",
        "python",
        "typescript",
    }
    assert all(version.endswith("/1") for version in document["compilerProviders"].values())
    required = {
        "language",
        "role",
        "extractor",
        "tier",
        "compiler_provider",
        "framework_adapters",
        "entry_detectors",
    }
    assert all(set(record) == required for record in capability_registry().values())
    assert capability_for("service.py").tier is CapabilityTier.SEMANTIC
    assert capability_for("client.js").tier is CapabilityTier.STRUCTURAL
    assert capability_for("unknown.future").tier is CapabilityTier.GENERIC
    validate_language_registry()


# 冻结注册表避免执行期间改变实现身份，重复名称则在组装时拒绝。
# 使用真实内置实现创建冲突，确保失败仅来自名字唯一性。
def test_extension_registry_is_immutable_and_rejects_duplicate_names() -> None:
    registry = builtin_extensions()
    with pytest.raises(TypeError):
        registry.compiler_providers["fake"] = registry.compiler_providers["python"]  # type: ignore[index]

    implementation = registry.structural_extractors["extract_python"].implementation
    duplicate = RegisteredImplementation("duplicate", "test/1", implementation)
    with pytest.raises(ConfigurationError, match="duplicate-extension-registration"):
        ExtensionRegistry.create(
            structural_extractors=(duplicate, duplicate),
            compiler_providers=registry.compiler_providers.values(),
            entry_detectors=registry.entry_detectors.values(),
            boundary_resolvers=registry.boundary_resolvers.values(),
        )
