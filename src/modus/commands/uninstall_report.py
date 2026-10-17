"""中文卸载预览：明确删除范围、知识保留策略与个人安装状态。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .reporting import (
    NOTICE_SEPARATOR,
    TreeNode,
    format_path_tree,
    render_tree,
)

__all__ = [
    "EMPTY_PERSONAL_NOTE",
    "KNOWLEDGE_DELETE_NOTICE",
    "KNOWLEDGE_RETAIN_NOTICE",
    "NOTICE_SEPARATOR",
    "PersonalReport",
    "deletes_knowledge",
    "format_personal_report",
    "format_project_header",
    "format_project_tree",
    "scope_notice_lines",
    "status_label",
]

KNOWLEDGE_DELETE_NOTICE = "警告：将删除 modus/knowledge 中的长期项目知识。"
KNOWLEDGE_RETAIN_NOTICE = "保留 modus/knowledge。如需同时删除，请取消后添加 --delete-knowledge。"
EMPTY_PERSONAL_NOTE = "未找到可卸载的个人 Modus 安装。"

_KNOWLEDGE_DIR_MARKERS = ("modus/knowledge",)
_STATUS_LABELS = {
    "planned": "待执行",
    "ok": "已完成",
    "permission-required": "需要权限",
}


# 将标题、路径树、空状态和警告分开，调用方可选择输出方式。
# 冻结的报告值避免渲染后被执行阶段意外修改。
@dataclass(frozen=True)
class PersonalReport:
    header: str
    tree: str
    empty_all_note: str
    warnings: tuple[str, ...]


# 未知或缺失状态按失败展示，避免把新状态默认解释为成功。
def status_label(status: str | None) -> str:
    if not status:
        return "失败"
    return _STATUS_LABELS.get(status, "失败")


# 这里只缩短展示路径；拼接后的文字不能用于执行命令。
def short_command(command: Sequence[str]) -> str:
    if not command:
        return ""
    parts = [Path(command[0]).name, *list(command[1:])]
    return " ".join(parts)


def _step_leaf(step: dict) -> TreeNode:
    label = status_label(str(step.get("status") or ""))
    command = short_command(list(step.get("command") or []))
    name = f"[{label}] {command}".strip()
    return TreeNode(name=name, note=str(step.get("message") or ""))


# 按目录边界匹配知识树，不将相似前缀误当作知识删除。
# 兼容预览与执行结果，供调用方统一决定是否强调风险。
def deletes_knowledge(result: dict | None) -> bool:
    """True when a project uninstall preview/result will remove the knowledge tree."""
    if not result:
        return False
    dirs = [str(path).rstrip("/") for path in (result.get("removedDirs") or [])]
    return any(
        path == marker or path.startswith(marker + "/")
        for path in dirs
        for marker in _KNOWLEDGE_DIR_MARKERS
    )


# 时态取决于 applied，而非删除列表是否非空。
# 文件、目录和受管区块独立计数，反映卸载的三种所有权粒度。
def format_project_header(result: dict) -> str:
    verb = "已删除" if result.get("applied") else "将删除"
    platforms = result.get("platforms") or []
    suffix = f"（平台： {', '.join(platforms)}）" if platforms else ""
    return (
        f"{verb}项目 Modus 资产：{len(result.get('removedFiles') or [])} 个文件，"
        f"{len(result.get('removedDirs') or [])} 个目录，"
        f"{len(result.get('removedSections') or [])} 个受管区块{suffix}"
    )


# 树形展示复用公共路径规则，不重新判断哪些文件可删除。
def format_project_tree(result: dict) -> str:
    return format_path_tree(
        result.get("removedFiles") or [],
        result.get("removedDirs") or [],
        result.get("removedSections") or [],
    )


# 个人卸载可能同时包含安装工具、启动器和缓存步骤。
# 警告即使在空结果中也保留，不能被“未找到安装”掩盖。
# 结果只描述已有步骤，不凭工具名称推导额外卸载动作。
def format_personal_report(
    cli_result: dict,
    *,
    apply: bool,
) -> PersonalReport:
    steps = [step for step in (cli_result.get("steps") or []) if isinstance(step, dict)]
    launchers = [str(path) for path in (cli_result.get("launchers") or [])]
    cache_step = cli_result.get("cache")
    branches: list[TreeNode] = []
    if steps:
        branches.append(TreeNode("CLI 工具", children=tuple(_step_leaf(step) for step in steps)))
    if launchers:
        branches.append(
            TreeNode("启动器", children=tuple(TreeNode(name=path) for path in launchers))
        )
    if isinstance(cache_step, dict):
        branches.append(TreeNode("uv 缓存", children=(_step_leaf(cache_step),)))
    warnings = tuple(str(item) for item in (cli_result.get("warnings") or []) if item)

    if not branches:
        return PersonalReport(
            header="",
            tree="",
            empty_all_note=EMPTY_PERSONAL_NOTE,
            warnings=warnings,
        )

    verb = "已卸载" if apply else "将卸载"
    return PersonalReport(
        header=f"{verb}个人 Modus：",
        tree=render_tree(tuple(branches)),
        empty_all_note="",
        warnings=warnings,
    )


# 返回强调行的索引，使展示层无需通过中文文字重新识别警告。
# 仅实际发现项目安装时才提示知识保留或删除策略。
# 工作区卸载的边界止于当前根目录，成员仓库需单独管理。
# 组合范围下即使项目跳过，也仍需说明个人安装处理范围。
# 范围选择来自调用方的已解析参数，本层不解析兼容别名。
def scope_notice_lines(
    scope: str,
    *,
    project_skipped: bool = False,
    project_result: dict | None = None,
) -> tuple[tuple[str, ...], int | None]:
    """Return notice lines and the index of the line that should be underlined."""
    lines: list[str] = []
    if scope == "combined":
        lines.append("卸载范围：项目与个人 Modus。")
        if project_skipped:
            lines.append("项目：未发现安装，已跳过；不会修改项目文件。")
        else:
            lines.append("项目：受管控制文件、平台接入文件与临时缓存。")
        lines.extend(
            [
                "个人：Modus CLI、启动器与此安装包的 uv 缓存。",
                "使用 --project 或 --person 仅卸载其中一项；--all 是默认行为的兼容别名。",
            ]
        )
    elif scope == "project":
        lines.append("卸载范围：仅项目资产，保留个人 CLI。")
    else:
        lines.append(
            "卸载范围：个人 CLI、启动器与此安装包的 uv 缓存。不读写项目文件。",
        )

    knowledge = (project_result or {}).get("knowledge") or {}
    installed = bool(project_result and project_result.get("status") != "not-installed")
    if scope != "personal" and installed:
        deleting = knowledge.get("disposition") == "delete"
        lines.append(KNOWLEDGE_DELETE_NOTICE if deleting else KNOWLEDGE_RETAIN_NOTICE)
        underline_at = len(lines) - 1
    else:
        underline_at = None
    if (
        scope != "personal"
        and installed
        and (project_result or {}).get("projectKind") == "workspace"
    ):
        lines.append("工作区卸载仅影响当前根目录，保留所有成员仓库。")
    if scope == "combined":
        underline_at = underline_at if underline_at is not None else len(lines) - 1
    return tuple(lines), underline_at
