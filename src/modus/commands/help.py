"""使用 Rich 公开 API 渲染助手工作流帮助，文本始终按字面显示。"""

from __future__ import annotations

from collections.abc import Iterable
from io import StringIO

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import command_reference as catalog

__all__ = ["PLATFORM_NOTE", "skill_surface_panels", "render_slash"]

PLATFORM_NOTE = "请在编程助手的对话中运行此工作流。"


def _panel(title: str, entries: Iterable[tuple[str, str]]) -> Panel:
    """按字面构建表格，避免将用户文本解释为标记。"""
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(ratio=1)
    for name, description in entries:
        table.add_row(Text(name), Text(description))
    return Panel(table, title=title, title_align="left", border_style="dim")


def skill_surface_panels() -> tuple[Panel, Panel]:
    """返回工作流面板，由调用方统一决定输出位置与宽度。"""
    return (
        _panel(catalog.PANEL_SLASH, ((item.name, item.short) for item in catalog.SLASH_COMMANDS)),
        _panel(catalog.PANEL_AUTOMATIC, catalog.AUTOMATIC_SKILL_HELP),
    )


# 先渲染到字符串，由外层决定打印位置；帮助构造本身不写全局终端。
def render_slash(slash: catalog.SlashCommand) -> str:
    """展示工作流摘要及可接收的材料类型。"""
    buffer = StringIO()
    console = Console(file=buffer)
    console.print(Text(f"用法：{slash.name} [输入材料]", style="bold"))
    console.print()
    console.print(Text(slash.detail_summary()))
    console.print(Text(PLATFORM_NOTE, style="dim"))
    if slash.inputs:
        console.print()
        console.print(_panel("输入材料", slash.inputs))
    return buffer.getvalue()
