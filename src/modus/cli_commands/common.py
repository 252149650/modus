"""作用域限定在 Modus 命令上的终端策略，不修改宿主进程的 Click 全局行为。"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from difflib import get_close_matches
from typing import TYPE_CHECKING, Any, TypeAlias

import click
import typer
from typer.core import TyperCommand, TyperGroup

from .. import command_reference as catalog
from .analysis_display import format_machine_error
from .presentation import ChineseHelpMixin, error_message

if TYPE_CHECKING:
    from ..machine.errors import MachineError

HELP_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


JsonObject: TypeAlias = dict[str, Any]


def command_panel(key: str) -> str:
    """读取目录中声明的命令帮助分组。"""
    return catalog.CLI_BY_KEY[key].panel


# 机器输出关闭颜色处理，调用者提供的文本按原样写入错误流。
def public_error(message: str, *, json_output: bool) -> None:
    if json_output:
        typer.echo(message, err=True)
    else:
        typer.secho(message, fg=typer.colors.RED, err=True)


# 契约、配置与预算属于可修正请求错误，其他分析失败使用执行错误码。
# JSON 保留原 details；中文恢复动作只应用于人类展示。
def machine_error(error: MachineError, *, json_output: bool) -> int:
    """保留机器错误契约，并返回稳定退出码。"""
    from ..machine.errors import BudgetError, ConfigurationError, ContractError

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "status": "error",
                    "error": {"code": error.code, "details": error.details},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            err=True,
        )
    else:
        typer.secho(format_machine_error(error), fg=typer.colors.RED, err=True)
    return 2 if isinstance(error, (ContractError, ConfigurationError, BudgetError)) else 3


# 输入输出都连接终端才允许交互，重定向到管道时不能等待确认。
def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class ModusGroup(ChineseHelpMixin, TyperGroup):
    """中文帮助与退出策略只作用于 Modus；命令建议仅包含公开入口。"""

    # 嵌入式调用保留 Click 的非独立模式，只有终端模式转成进程退出。
    # 捕获 Click 已分类错误，避免修改第三方全局异常格式器。
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        kwargs = dict(
            prog_name=prog_name,
            complete_var=complete_var,
            windows_expand_args=windows_expand_args,
            **extra,
        )
        if not standalone_mode:
            return super().main(args=args, standalone_mode=False, **kwargs)
        try:
            result = super().main(args=args, standalone_mode=False, **kwargs)
        except click.exceptions.NoArgsIsHelpError as exc:
            click.echo(exc.ctx.get_help() if exc.ctx else exc.message, err=True)
            raise SystemExit(exc.exit_code) from exc
        except click.ClickException as exc:
            click.secho(f"错误：{error_message(exc)}", fg="red", err=True)
            if isinstance(exc, click.UsageError):
                command_path = (
                    exc.ctx.command_path if exc.ctx is not None else (prog_name or "modus")
                )
                click.echo(f"运行 {command_path} --help 查看用法。", err=True)
            raise SystemExit(exc.exit_code) from exc
        except click.Abort as exc:
            click.echo("已取消操作。", err=True)
            raise SystemExit(1) from exc
        raise SystemExit(result if isinstance(result, int) else 0)

    # 有限的优先顺序服务初读导航，其余扩展仍按名称稳定排序。
    def list_commands(self, ctx: click.Context) -> list[str]:
        names = super().list_commands(ctx)
        if ctx.parent is None:
            preferred: tuple[str, ...] = (
                "init",
                "update",
                "help",
                "uninstall",
                "status",
            )
        else:
            preferred = ("build",)
        positions = {name: index for index, name in enumerate(preferred)}
        return sorted(names, key=lambda name: (positions.get(name, len(preferred)), name))

    # 拼写建议是原 UsageError 的展示补充，异常类别和退出码保持不变。
    # 候选明确排除 hidden 命令，内部工具不会通过相似匹配出现在帮助中。
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            # 直接使用 Click 的公开分发 API，避免依赖 Typer 的私有兼容别名。
            return click.Group.resolve_command(self, ctx, args)
        except click.exceptions.UsageError as exc:
            if args:
                public = []
                for name in self.list_commands(ctx):
                    command = self.get_command(ctx, name)
                    if command is not None and not command.hidden:
                        public.append(name)
                matches = get_close_matches(args[0], public, n=1)
                if matches:
                    exc.message = f"未知命令：{args[0]}。你是否想使用 {matches[0]}？"
            raise


class ModusCommand(ChineseHelpMixin, TyperCommand):
    """共享中文帮助策略的叶子命令。"""
