"""显式观察安装与知识状态；状态缺失不构成任务执行门槛。"""

from dataclasses import dataclass

from rich.console import Console

from ..display_labels import display_label

__all__ = ["StatusView", "build_status_view", "echo_status", "project_status"]


def project_status(root: str) -> dict:
    """Collect the preflight report; imported lazily to keep CLI startup light."""
    from ..orchestrator.preflight import check

    return check(root)


@dataclass(frozen=True)
class StatusView:
    """Presentation-ready status lines, separated from terminal I/O."""

    lines: tuple[str, ...]


# 未安装但知识仍在是合法状态，不能把用户知识误判成残留错误。
def build_status_view(report: dict) -> StatusView:
    """Translate the preflight report into the human status summary."""
    installed = report.get("initialized", False)
    update = report.get("projectUpdate", {})
    knowledge_files = int(report.get("knowledge", {}).get("files", 0) or 0)
    if installed:
        installation_line = "项目接入：已安装"
    elif knowledge_files:
        installation_line = (
            "项目接入：未安装，已创作的知识仍在。运行 `modus init` 重新接入项目资产。"
        )
    else:
        installation_line = "项目接入：未安装；运行 `modus init` 接入。"
    lines = [
        f"项目：{report.get('root', '.')}",
        installation_line,
        f"知识文档： {knowledge_files}",
    ]
    if update.get("needsUpdate"):
        lines.append(
            f"接入更新： {display_label(str(update.get('status') or 'unknown'))} (modus update)"
        )
    if update.get("error"):
        lines.append(f"接入清单： {update['error']}")
    return StatusView(tuple(lines))


# 路径和诊断按字面输出，避免其中的方括号被 Rich 当作样式。
def echo_status(view: StatusView, *, stream=None) -> None:
    console = Console(file=stream, soft_wrap=True)
    for line in view.lines:
        console.print(line, markup=False, highlight=False)
