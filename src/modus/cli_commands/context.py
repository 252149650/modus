"""当前项目证据的只读检索入口。"""

from __future__ import annotations

import json

import typer

from ..command_reference import cli_help
from ..commands import context_cmd
from ..orchestrator import context_resolver
from .common import (
    HELP_CONTEXT_SETTINGS,
    ModusCommand,
    command_panel,
    public_error,
)

context_app = typer.Typer(
    no_args_is_help=True,
    help="只读解析项目上下文。",
    context_settings=HELP_CONTEXT_SETTINGS,
)


_CONTEXT_PATH_OPTION = typer.Option([], "--path", help="明确指定的项目相对路径，可重复指定。")


# 此适配器验证终端参数，范围计算与检索策略仍归 context_resolver。
# 显式路径越界属于参数错误，不应被包装成一般执行故障。
# 当前接口只提供 JSON，便于助手读取证据与缺口的完整结构。
# 展示序列化保留中文内容，协议键不作本地化转换。
# project-read 的只读保证由服务维护，终端入口不创建索引或缓存。
@context_app.command(
    cls=ModusCommand,
    name="resolve",
    help=cli_help("context resolve"),
    rich_help_panel=command_panel("context resolve"),
)
def context_resolve_cmd(
    request: str = typer.Argument(..., help="任务描述；project-read 不会写入磁盘。"),
    scope: str = typer.Option(..., "--scope", help="project-read：从当前文件执行只读检索。"),
    purpose: str | None = typer.Option(None, "--purpose", help="可选的证据用途：change（变更）。"),
    path: list[str] = _CONTEXT_PATH_OPTION,
    root: str = typer.Option(".", "--root", help="项目根目录。"),
    format_: str = typer.Option("json", "--format", help="输出格式，仅支持 json。"),
) -> None:
    """Resolve Domains, knowledge, and current source without writing files."""
    if format_.lower() != "json":
        raise typer.BadParameter("仅支持 json 格式", param_hint="--format")
    if scope not in context_resolver.VALID_SCOPES:
        raise typer.BadParameter("请使用 project-read", param_hint="--scope")
    try:
        result = context_cmd.resolve(
            root, request, scope=scope, explicit_paths=path, purpose=purpose
        )
    except context_resolver.PathOutsideRootError as exc:
        raise typer.BadParameter(str(exc), param_hint="--path") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        public_error(str(exc), json_output=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
