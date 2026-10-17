"""公开帮助与安装状态的只读终端入口。"""

from __future__ import annotations

import json
from difflib import get_close_matches
from typing import cast

import click
import typer

from .. import command_reference as catalog
from ..commands import help as help_pages
from ..commands import status as status_cmd


def _child_context(command: click.Command, name: str, parent: click.Context) -> click.Context:
    return click.Context(command, info_name=name, parent=parent)


# 递归遍历真实注册树，隐藏入口不参与命令建议。
# 每个临时 Context 都被关闭，帮助发现不能遗留上下文资源。
def _public_command_paths(root: click.Command, root_ctx: click.Context) -> list[str]:
    paths: list[str] = []

    def visit(command: click.Command, ctx: click.Context, prefix: tuple[str, ...]) -> None:
        for name in command.list_commands(ctx) if hasattr(command, "list_commands") else ():
            child = cast(click.Group, command).get_command(ctx, name)
            if child is None or child.hidden:
                continue
            path = (*prefix, name)
            paths.append(" ".join(path))
            child_ctx = _child_context(child, name, ctx)
            try:
                visit(child, child_ctx, path)
            finally:
                child_ctx.close()

    visit(root, root_ctx, ())
    return paths


# 逐级查找保留子命令上下文，参数帮助由所属命令生成。
# 逆序关闭与创建顺序对称，异常路径也不泄漏上下文。
def _public_help_for_path(root_ctx: click.Context, tokens: list[str]) -> str:
    command = root_ctx.command
    current_ctx = root_ctx
    child_contexts: list[click.Context] = []
    try:
        for token in tokens:
            if not hasattr(command, "get_command"):
                raise LookupError
            child = command.get_command(current_ctx, token)
            if child is None or child.hidden:
                raise LookupError
            child_ctx = _child_context(child, token, current_ctx)
            child_contexts.append(child_ctx)
            command = child
            current_ctx = child_ctx
        return command.get_help(current_ctx)
    finally:
        for child_ctx in reversed(child_contexts):
            child_ctx.close()


# 相似建议只从公开命令和显式工作流产生，不能泄露内部入口。
def _unknown_help_topic(root_ctx: click.Context, raw: str) -> None:
    candidates = [
        *_public_command_paths(root_ctx.command, root_ctx),
        *(slash.name for slash in catalog.SLASH_COMMANDS),
    ]
    matches = get_close_matches(raw, candidates, n=1)
    typer.secho(f"未知的公开命令： {raw}", fg=typer.colors.RED, err=True)
    if matches:
        typer.echo(f"你可能想查看： modus help {matches[0]}", err=True)
    raise typer.Exit(2)


_HELP_TOPIC_ARGUMENT = typer.Argument(None, help="公开命令路径或工作流命令，例如 /modus-init。")


# 助手工作流不是终端子命令，先按独立目录解析，再查实际 CLI。
# 缺少主题时复用根帮助，避免维护两套首页文案。
def help_cmd(
    ctx: typer.Context,
    topic: list[str] = _HELP_TOPIC_ARGUMENT,
) -> None:
    """查看公开终端命令或助手工作流帮助。"""
    root_ctx = ctx.parent
    if root_ctx is None:  # pragma: no cover - Typer always provides the root context
        raise typer.Exit(2)
    if not topic:
        typer.echo(root_ctx.get_help())
        return
    raw = " ".join(topic)
    slash = catalog.lookup_slash(raw)
    if slash is not None:
        typer.echo(help_pages.render_slash(slash), nl=False)
        return
    try:
        typer.echo(_public_help_for_path(root_ctx, topic))
    except LookupError:
        _unknown_help_topic(root_ctx, raw)


# 状态观察失败不阻断用户继续使用项目，返回非零码并给简短诊断。
# JSON 仅暴露异常类别，避免把未经控制的内部详情写进公共报告。
def status(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出完整 JSON，供助手、脚本与 CI 使用。",
    ),
) -> None:
    """查看安装与已创作知识的状态。"""
    try:
        report = status_cmd.project_status(".")
    except Exception as exc:  # noqa: BLE001 - status must remain a traceback-free surface
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "status": "internal-error",
                        "errorType": type(exc).__name__,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            typer.echo("暂时无法读取项目状态，当前项目工具仍可使用。")
        raise typer.Exit(1) from None
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False))
        return
    status_cmd.echo_status(status_cmd.build_status_view(report))
