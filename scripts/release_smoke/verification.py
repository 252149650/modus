"""Apply declarative public-release policy to normalized archive inventories."""

from __future__ import annotations

import re
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from email.parser import Parser

from .contracts import (
    COMMAND_NAMES,
    CONSOLE_SCRIPT,
    FORBIDDEN_DEPENDENCIES,
    PACKAGE_BOUNDARIES,
    PUBLIC_ARCHIVE_EXCLUSIONS,
    SDIST_EXCLUSIONS,
    SDIST_PACKAGE_BOUNDARIES,
    SDIST_ROOT_BOUNDARY,
    WHEEL_METADATA_HEADERS,
    WORKFLOW_NAMES,
    ArchitectureBoundary,
    ArtifactInventory,
    ArtifactKind,
    ArtifactVerification,
    ReleaseCheckError,
)
from .diagnostics import format_paths, require

_DISTRIBUTION_NAME = re.compile(r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")


# 按能力名称报告缺失文件，使打包问题可定位到架构职责。
# 清单已经归一化，本层不猜测包内目录前缀。
def _verify_boundary(
    inventory: ArtifactInventory,
    boundary: ArchitectureBoundary,
) -> None:
    missing = boundary.members - inventory.members
    require(
        not missing,
        f"{inventory.path.name} 缺少 {boundary.name} boundary "
        f"from its {inventory.kind.value}:\n{format_paths(missing)}",
    )


# 依赖名按 Python 分发名规则统一连字符、点和下划线。
# 仅比较名称部分，版本约束和环境标记不影响禁用依赖判断。
def _canonical_distribution_name(requirement: str) -> str:
    match = _DISTRIBUTION_NAME.match(requirement.strip())
    return re.sub(r"[-_.]+", "-", match.group().lower()) if match else ""


# 先验证元数据结构，再核验公开声明与控制台入口。
# 入口解析禁用插值，避免配置语法改变原始入口值。
# README 正文与许可证文件均需实际进入产物。
# 最低 Python 已支持的标准库能力不应再引入旧兼容依赖。
def _verify_wheel_metadata(inventory: ArtifactInventory) -> None:
    if inventory.metadata is None:
        raise ReleaseCheckError(f"{inventory.path.name} 缺少包元数据")
    metadata = Parser().parsestr(inventory.metadata)
    require(
        not metadata.defects,
        f"{inventory.path.name} 的包元数据格式无效：{metadata.defects!r}",
    )

    mismatches = {
        header: (expected, metadata.get(header))
        for header, expected in WHEEL_METADATA_HEADERS
        if metadata.get(header) != expected
    }
    require(
        not mismatches,
        f"{inventory.path.name} 的元数据头错误："
        + "; ".join(
            f"{header} expected {expected!r}, found {actual!r}"
            for header, (expected, actual) in sorted(mismatches.items())
        ),
    )

    dependencies = metadata.get_all("Requires-Dist", [])
    forbidden_dependencies = {
        dependency
        for dependency in dependencies
        if _canonical_distribution_name(dependency) in FORBIDDEN_DEPENDENCIES
    }
    require(
        not forbidden_dependencies,
        f"{inventory.path.name} 要求 Python >=3.11 时不能依赖 tomli",
    )
    description = metadata.get_payload()
    require(
        isinstance(description, str) and bool(description.strip()),
        f"{inventory.path.name} 元数据中的 README 描述为空",
    )
    require(
        any(member.endswith(".dist-info/licenses/LICENSE") for member in inventory.members),
        f"{inventory.path.name} 未在 .dist-info/licenses 中包含项目 LICENSE",
    )

    if inventory.entry_points is None:
        raise ReleaseCheckError(f"{inventory.path.name} 缺少入口元数据")
    entry_points = ConfigParser(interpolation=None)
    try:
        entry_points.read_string(inventory.entry_points)
        script_name, expected_target = CONSOLE_SCRIPT
        actual_target = entry_points.get("console_scripts", script_name)
    except (ConfigParserError, KeyError) as exc:
        raise ReleaseCheckError(f"{inventory.path.name} 的终端脚本元数据无效：{exc}") from exc
    require(
        actual_target == expected_target,
        f"{inventory.path.name} 终端脚本 {script_name!r} 必须指向 "
        f"{expected_target!r}, found {actual_target!r}",
    )


# 字节码与退役目录都属于构建污染，不能随源码一起发布。
# 规则检查成员路径，不依赖压缩包内文件的时间戳。
def _verify_archive_boundaries(inventory: ArtifactInventory) -> None:
    compiled = {
        member
        for member in inventory.members
        if member.endswith(".pyc") or "__pycache__/" in member
    }
    require(
        not compiled,
        f"{inventory.path.name} 包含编译后的 Python 产物：\n{format_paths(compiled)}",
    )

    excluded = {
        member for member in inventory.members if PUBLIC_ARCHIVE_EXCLUSIONS.excludes(member)
    }
    require(
        not excluded,
        f"{inventory.path.name} 暴露了已退役或仅供仓库使用的接口：\n{format_paths(excluded)}",
    )


# 同时拒绝缺失与多余工作流，防止内部入口意外公开。
# 只取工作流根下第一层身份，嵌套参考文档仍属于原工作流。
def _verify_workflows(inventory: ArtifactInventory) -> None:
    shipped = frozenset(
        member.removeprefix(inventory.workflow_root).split("/", 1)[0]
        for member in inventory.members
        if member.startswith(inventory.workflow_root)
    )
    missing = WORKFLOW_NAMES - shipped
    unexpected = shipped - WORKFLOW_NAMES
    violations = []
    if missing:
        violations.append("missing:\n" + format_paths(missing))
    if unexpected:
        violations.append("unexpected:\n" + format_paths(unexpected))
    require(
        not violations,
        f"{inventory.path.name} 违反内置工作流契约\n" + "\n".join(violations),
    )


# 显式命令集合与技能集合分别验证，避免二者误相互替代。
# 只把命令根下的 Markdown 文件视作可发布命令。
def _verify_commands(inventory: ArtifactInventory) -> None:
    command_root = inventory.workflow_root.removesuffix("skills/") + "commands/"
    shipped = frozenset(
        relative.removesuffix(".md")
        for member in inventory.members
        for relative in [member.removeprefix(command_root)]
        if member.startswith(command_root) and relative.endswith(".md") and "/" not in relative
    )
    require(
        shipped == COMMAND_NAMES,
        f"{inventory.path.name} violates the explicit command contract: "
        f"expected {sorted(COMMAND_NAMES)!r}, found {sorted(shipped)!r}",
    )


# 格式专属契约先执行，再检查两种归档共有的公开边界。
# 源码包需具备维护入口，wheel 需具备安装元数据。
# 任何阶段失败均保留归档名称与具体原因。
# 文件大小从实际产物读取，成功报告不使用预估值。
# 结果只表达本次核验完成，不代替后续安装场景验收。
def verify_artifact(inventory: ArtifactInventory) -> ArtifactVerification:
    """Apply format-specific and shared public-release contracts."""

    if inventory.kind is ArtifactKind.WHEEL:
        for boundary in PACKAGE_BOUNDARIES:
            _verify_boundary(inventory, boundary)
        _verify_wheel_metadata(inventory)
    else:
        _verify_boundary(inventory, SDIST_ROOT_BOUNDARY)
        for boundary in SDIST_PACKAGE_BOUNDARIES:
            _verify_boundary(inventory, boundary)
        excluded = {member for member in inventory.members if SDIST_EXCLUSIONS.excludes(member)}
        require(
            not excluded,
            f"{inventory.path.name} 包含仅供仓库使用的源码成员：\n{format_paths(excluded)}",
        )

    _verify_archive_boundaries(inventory)
    _verify_workflows(inventory)
    _verify_commands(inventory)
    try:
        artifact_size = inventory.path.stat().st_size
    except OSError as exc:
        raise ReleaseCheckError(f"无法读取产物状态：{inventory.path}: {exc}") from exc
    return ArtifactVerification(
        path=inventory.path,
        member_count=len(inventory.members),
        size_bytes=artifact_size,
        workflow_count=len(WORKFLOW_NAMES),
    )
