"""中文终端展示边界。

只依赖 Click 的公开契约与 Rich 展示组件；不修改第三方全局状态，
不翻译命令、路径、协议字段或用户输入。帮助、确认与解析错误由此统一呈现。
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from io import StringIO
from typing import cast

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import command_reference as catalog
from ..commands.help import skill_surface_panels


# 仅替换框架固定占位词，选项名和用户提供的取值保持原样。
def _metavar(parameter: click.Parameter, ctx: click.Context) -> str:
    value = parameter.make_metavar(ctx)
    names = {
        "TEXT": "文本",
        "PATH": "路径",
        "DIRECTORY": "目录",
        "INTEGER": "整数",
        "FLOAT": "数值",
    }
    return re.sub(r"\b(?:TEXT|PATH|DIRECTORY|INTEGER|FLOAT)\b", lambda m: names[m[0]], value)


# 双向布尔选项展示正反名称；无值开关不添加多余占位符。
def _parameter_label(parameter: click.Parameter, ctx: click.Context) -> str:
    if isinstance(parameter, click.Argument):
        return _metavar(parameter, ctx)
    label = ", ".join(parameter.opts)
    if isinstance(parameter, click.Option):
        if parameter.secondary_opts:
            label += " / " + ", ".join(parameter.secondary_opts)
        if not parameter.is_flag and not parameter.count:
            label += " " + _metavar(parameter, ctx)
    return label


# 读取默认值时禁止调用工厂，查看帮助不应触发业务副作用。
# 必填与可重复标记从参数契约推导，避免帮助文案重复维护。
def _parameter_description(parameter: click.Parameter, ctx: click.Context) -> str:
    parts = [getattr(parameter, "help", None) or ""]
    if parameter.required:
        parts.append("[必填]")
    if parameter.multiple or parameter.nargs == -1:
        parts.append("[可重复]")
    default = parameter.get_default(ctx, call=False)
    if (
        default is not None
        and default != ()
        and default != []
        and not callable(default)
        and not (isinstance(parameter, click.Option) and parameter.is_flag)
    ):
        if isinstance(default, bool):
            default = "是" if default else "否"
        parts.append(f"[默认：{default}]")
    return " ".join(part for part in parts if part)


def _panel(title: str, rows: list[tuple[str, str]]) -> Panel:
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(ratio=1)
    for name, description in rows:
        table.add_row(Text(name), Text(description))
    return Panel(table, title=title, title_align="left", border_style="dim")


def format_help(command: click.Command, ctx: click.Context, formatter: click.HelpFormatter) -> None:
    """从实际注册的命令与参数生成帮助，目录只负责补充工作流语义。"""
    buffer = StringIO()
    console = Console(file=buffer, width=formatter.width, color_system=None)
    is_group = isinstance(command, click.Group)
    suffix = "[全局选项] 命令 [参数]..." if ctx.parent is None else "[选项]"
    if ctx.parent is not None and is_group:
        suffix += " 命令 [参数]..."
    console.print(Text(f"用法：{ctx.command_path} {suffix}", style="bold"))
    if command.help:
        console.print()
        console.print(Text(command.help))
    sections = _help_sections(command, ctx)
    for title, rows in sections.items():
        console.print()
        console.print(_panel(title, rows))
    if ctx.parent is None:
        for panel in skill_surface_panels():
            console.print()
            console.print(panel)
    if command.epilog:
        console.print(Text(command.epilog))
    formatter.write(buffer.getvalue())


def _help_sections(command: click.Command, ctx: click.Context) -> dict[str, list[tuple[str, str]]]:
    """从注册参数与子命令生成分组，面板渲染不参与命令发现。"""
    sections: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for parameter in command.get_params(ctx):
        if getattr(parameter, "hidden", False):
            continue
        title = getattr(parameter, "rich_help_panel", None)
        if not title:
            title = "参数" if isinstance(parameter, click.Argument) else "选项"
        sections[title].append(
            (_parameter_label(parameter, ctx), _parameter_description(parameter, ctx))
        )
    # 帮助始终可发现；不依赖 Typer 的私有 rich_utils 常量。
    sections["全局选项" if ctx.parent is None else "选项"].append(
        ("-h, --help", catalog.HELP_OPTION_TEXT)
    )
    if isinstance(command, click.Group):
        assert isinstance(command, click.Group)
        for name in command.list_commands(ctx):
            child = command.get_command(ctx, name)
            if child is None or child.hidden:
                continue
            title = getattr(child, "rich_help_panel", None) or "命令"
            sections[title].append((name, child.short_help or (child.help or "").split("\n", 1)[0]))
    return sections


# 空输入严格采用调用者给定默认值，破坏性操作可统一默认拒绝。
# 沿用 Click 输入流，使取消与测试运行器行为一致。
def confirm(message: str, *, default: bool = False) -> bool:
    """保持 Click 的输入流与中断语义，提供中文选择及重试提示。"""
    suffix = "[是/否，默认：是]" if default else "[是/否，默认：否]"
    while True:
        answer = (
            click.prompt(f"{message} {suffix}", default="", show_default=False).strip().casefold()
        )
        if not answer:
            return default
        if answer in {"y", "yes", "是", "好", "确认"}:
            return True
        if answer in {"n", "no", "否", "不", "取消"}:
            return False
        click.echo("请输入 是/否（也支持 y/n）。", err=True)


def _parameter_hint(error: click.BadParameter) -> str:
    hint = error.param_hint
    if hint is None and error.param is not None and error.ctx is not None:
        hint = error.param.get_error_hint(error.ctx)
    if hint is None:
        return "参数"
    return hint if isinstance(hint, str) else " / ".join(hint)


# 只对整句匹配成功的框架错误做翻译，未知错误保留原文。
# 捕获的路径和取值直接带回，避免翻译改变可操作的信息。
def _parser_detail(message: str) -> str:
    """仅适配框架固定句式；动态值原样保留，避免误改文件名和用户输入。"""
    # Click 对路径使用 repr；Windows 的反斜线和文件名中的引号需按字符串字面量还原。
    descriptions = {
        "does not exist": "路径不存在",
        "is not a directory": "路径不是目录",
        "is a file": "路径不是目录",
        "is not a file": "路径不是文件",
        "is a directory": "路径不是文件",
        "is not readable": "路径不可读",
        "is not writable": "路径不可写",
        "is not executable": "路径不可执行",
    }
    for suffix, description in descriptions.items():
        match = re.fullmatch(rf"(?:Directory|File|Path) (.+) {re.escape(suffix)}\.", message)
        if match is None:
            continue
        try:
            path = ast.literal_eval(match[1])
        except (SyntaxError, ValueError):
            return message
        if isinstance(path, str):
            return f"{description}：{path}"
    patterns = (
        (r"No such command '(.*)'\.", r"未知命令：\1。"),
        (r"Option '(.*)' requires an argument\.", r"选项 \1 缺少参数值。"),
        (r"Got unexpected extra argument(?:s)? \((.*)\)", r"存在多余参数：\1"),
        (r"'(.*)' is not a valid integer\.", r"\1 不是有效整数。"),
        (r"'(.*)' is not a valid float\.", r"\1 不是有效数值。"),
        (r"'(.*)' is not one of (.*)\.", r"\1 不在可选值中：\2。"),
        (r"(.*) is not in the range (.*)\.", r"\1 不在允许范围内：\2。"),
    )
    for pattern, replacement in patterns:
        if re.fullmatch(pattern, message):
            return re.sub(pattern, replacement, message)
    return message


# 优先使用异常类型中的结构化字段，再回退到固定句式适配。
def error_message(error: click.ClickException) -> str:
    if isinstance(error, click.MissingParameter):
        return f"缺少必填参数：{_parameter_hint(error)}"
    if isinstance(error, click.NoSuchOption):
        return f"未知选项：{error.option_name}。" + (
            f"你是否想使用：{'、'.join(error.possibilities)}？" if error.possibilities else ""
        )
    if isinstance(error, click.BadParameter):
        return f"参数 {_parameter_hint(error)} 无效：{_parser_detail(error.message)}"
    return _parser_detail(error.message)


# 本地化作用域限定为 Modus 命令实例，不修改 Click 全局函数。
# 隐藏默认帮助项是为了由公共展示器统一输出一次。
class ChineseHelpMixin:
    """只覆盖 Modus 命令实例；宿主应用中的其他 Click 命令不受影响。"""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        format_help(cast(click.Command, self), ctx, formatter)

    def get_help_option(self, ctx: click.Context) -> click.Option | None:
        option = click.Command.get_help_option(cast(click.Command, self), ctx)
        if option is not None:
            option.help = catalog.HELP_OPTION_TEXT
            option.hidden = True
        return option
