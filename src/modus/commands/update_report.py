"""中文更新预览：结构化结果转为路径树、范围说明与执行状态。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .reporting import NOTICE_SEPARATOR, TreeNode, format_path_tree, render_tree

__all__ = [
    "NOTICE_SEPARATOR",
    "PersonalReport",
    "format_personal_report",
    "format_project_header",
    "format_project_tree",
    "personal_has_work",
    "project_has_work",
    "scope_notice_lines",
    "skipped_project_paths",
]

_WRITE_SELF_STATUSES = frozenset({"dry-run", "upgraded"})


# 不可变报告只承载展示内容，不保留可执行的升级对象。
@dataclass(frozen=True)
class PersonalReport:
    header: str
    tree: str
    empty_note: str


# 统计实际变更分类；被保护而跳过的文件不计入待更新数量。
def _file_count(result: dict) -> int:
    return (
        len(result.get("appliedFiles") or [])
        + len(result.get("addedFiles") or [])
        + len(result.get("removedFiles") or [])
    )


# 文件标签与预览/执行时态保持一致。
# 路径树与摘要共同读取服务结果，避免再次扫描带来的时间差。
def _file_notes(result: dict, *, apply: bool) -> dict[str, str]:
    updated = "[已更新]" if apply else "[将更新]"
    added = "[已新增]" if apply else "[将新增]"
    removed = "[已删除]" if apply else "[将删除]"
    notes: dict[str, str] = {}
    for path in result.get("appliedFiles") or []:
        notes[str(path)] = updated
    for path in result.get("addedFiles") or []:
        notes[str(path)] = added
    for path in result.get("removedFiles") or []:
        notes[str(path)] = removed
    return notes


# 先处理失败状态，再判断是否无更新，避免空列表掩盖错误。
# 平台列表只影响说明，不改变更新范围。
def format_project_header(result: dict, *, apply: bool) -> str:
    status = str(result.get("status") or "")
    if status in {"error", "blocked"}:
        return str(result.get("message") or "项目更新失败。")
    if status in {"up-to-date", "noop"} and not _file_count(result):
        return "项目技能已是最新版本。"
    verb = "已更新" if apply else "将更新"
    platforms = [str(item) for item in (result.get("platforms") or []) if item]
    suffix = f"（平台： {', '.join(platforms)}）" if platforms else ""
    return f"{verb}项目技能：{_file_count(result)} 个文件{suffix}"


# 删除目录独立传入树形渲染器，保留文件与目录的视觉区分。
def format_project_tree(result: dict, *, apply: bool = False) -> str:
    files = [
        *[str(path) for path in (result.get("appliedFiles") or [])],
        *[str(path) for path in (result.get("addedFiles") or [])],
        *[str(path) for path in (result.get("removedFiles") or [])],
    ]
    dirs = [str(path) for path in (result.get("removedDirectories") or [])]
    return format_path_tree(files, dirs, (), file_notes=_file_notes(result, apply=apply))


# 保留首次出现的顺序，让冲突报告稳定可读。
# 人工编辑与冲突可能重叠，同一路径只需提醒一次。
def skipped_project_paths(result: dict) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            [
                *[str(path) for path in (result.get("userEditedFiles") or [])],
                *[str(path) for path in (result.get("conflictedFiles") or [])],
            ]
        )
    )


# 仅用于展示，执行仍应使用原始参数列表而非此字符串。
def _upgrade_display(command: Sequence[str]) -> str:
    if not command:
        return "Modus CLI"
    return " ".join((Path(command[0]).name, *command[1:]))


# 可编辑安装有独立说明，不把跳过升级解释为失败。
# 权限不足与工具错误分开显示，便于用户判断下一步。
# 预览不声称升级已完成，即使计划中已包含完整命令。
def format_personal_report(self_update_report: dict, *, apply: bool) -> PersonalReport:
    status = str(self_update_report.get("status") or "")
    if status == "disabled":
        return PersonalReport(header="", tree="", empty_note="")

    if status == "skipped-editable":
        header = "已检查个人 CLI：" if apply else "将升级个人 CLI："
        return PersonalReport(
            header=header,
            tree="",
            empty_note="当前为可编辑安装，已跳过 CLI 安装包升级。",
        )

    command = [str(item) for item in (self_update_report.get("command") or [])]
    if apply and status == "current":
        header = "已检查个人 CLI："
        label = "已完成"
    elif apply and status == "upgraded":
        header = "已升级个人 CLI："
        label = "已完成"
    elif apply and status == "permission-required":
        header = "个人 CLI 操作需要权限："
        label = "需要权限"
    elif apply and status == "error":
        header = "个人 CLI 升级失败："
        label = "失败"
    else:
        header = "将升级个人 CLI："
        label = "待执行"

    leaf = f"[{label}] {_upgrade_display(command)}"
    tree = render_tree((TreeNode("CLI 工具", children=(TreeNode(name=leaf),)),))
    return PersonalReport(header=header, tree=tree, empty_note="")


# 被阻止的项目更新不进入待执行清单，但错误仍由上层报告。
def project_has_work(result: dict | None) -> bool:
    if result is None:
        return False
    status = str(result.get("status") or "")
    if status in {"error", "blocked"}:
        return False
    return status not in {"up-to-date", "noop"} or bool(_file_count(result))


# 只有明确的可写状态算作个人升级工作，未知状态默认不执行。
def personal_has_work(self_update_report: dict | None) -> bool:
    if not self_update_report:
        return False
    return str(self_update_report.get("status") or "") in _WRITE_SELF_STATUSES


# 范围说明与执行逻辑共享 project/personal/combined 的边界。
# 强调行用索引返回，调用方不依赖对显示文字做匹配。
# 项目未接入时仍保留跳过说明，组合更新可以继续处理个人 CLI。
def scope_notice_lines(
    scope: str, *, project_skipped: bool = False
) -> tuple[tuple[str, ...], int | None]:
    prefix = ("项目：未找到有效的 Modus 接入信息，已跳过。",) if project_skipped else ()
    if scope == "combined":
        lines = prefix + (
            "`modus update` 默认执行以下两个阶段。",
            "安装包升级：升级个人 Modus CLI。",
            "技能更新：同步受管命令、技能与平台接入文件。",
            "使用 `modus update --project` 仅更新项目技能；"
            "使用 `modus update --person` 仅升级个人 CLI。",
        )
        return lines, len(lines) - 1
    if scope == "project":
        return (
            prefix + ("更新范围：仅项目技能，保留个人 CLI。",),
            None,
        )
    return (
        prefix + ("更新范围：仅个人 CLI，保留项目资产。",),
        None,
    )
