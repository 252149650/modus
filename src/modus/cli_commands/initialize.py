"""在终端边界完成平台选择与项目接入。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer

from .. import (
    git_hygiene,
    workspace,
)
from ..artifacts_io import CorruptArtifactError
from ..commands import (
    init_project,
    init_report,
)
from ..manifest import Manifest
from ..paths import ModusPaths
from ..platforms import available_platforms
from ..platforms.display import ordered_platforms, platform_title
from .common import is_interactive
from .presentation import confirm

_INIT_MEMBER_OPTION = typer.Option(
    [], "--member", help="工作区成员的 Git 根目录，相对于当前目录，可重复指定。"
)


_INIT_PLATFORM_OPTION = typer.Option([], "--platform", help="要接入的平台，可重复指定。")


# 把选择结果与非交互回退提示一起返回，避免解析过程直接写终端。
@dataclass(frozen=True, slots=True)
class InitPlatformSelection:
    """已解析的接入目标，以及非交互模式下需要展示的提示。"""

    platforms: list[str]
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformPromptChoice:
    """平台多选项的展示模型，协议值与显示名称各自独立。"""

    value: str
    title: str
    checked: bool = True


def _ordered_available_platforms() -> list[str]:
    return ordered_platforms(available_platforms())


def _platform_prompt_choices() -> list[PlatformPromptChoice]:
    return [
        PlatformPromptChoice(
            value=name,
            title=platform_title(name),
            checked=True,
        )
        for name in _ordered_available_platforms()
    ]


# 交互依赖只在确实需要选择时加载，显式参数路径不依赖提示组件。
# 第三方选择控件返回协议值，中文标题只用于展示。
def _prompt_platforms(choices: list[PlatformPromptChoice]) -> list[str] | None:
    try:
        import questionary
        from questionary import Choice
    except ImportError as exc:
        raise ValueError(
            "交互式 modus init 需要 questionary；"
            "请安装 Modus 依赖，或指定 --platform <名称> / --all-platforms"
        ) from exc

    selected = questionary.checkbox(
        "请选择要接入的平台：",
        choices=[
            Choice(title=choice.title, value=choice.value, checked=choice.checked)
            for choice in choices
        ],
        instruction=("（空格：切换，a：全选/取消，i：反选，回车：确认）"),
        validate=lambda selected: True if selected else "请至少选择一个平台。",
        initial_choice=choices[0].value if choices else None,
        # 由我们输出最终回执，避免第三方固定的英文完成提示留在终端。
        erase_when_done=True,
    ).ask(kbi_msg="已取消平台选择。")
    if selected:
        typer.echo("已选择平台：" + "、".join(platform_title(name) for name in selected))
    return selected


# 取消与空选择分别报错，不能回退为默认全平台。
# 去重保留用户选择顺序，未知平台在写入前拒绝。
def _validate_selected_platforms(selected: list[str] | None) -> list[str]:
    if selected is None:
        raise ValueError("已取消平台选择")
    if not selected:
        raise ValueError("请至少选择一个平台")

    choices = set(available_platforms())
    unknown = [name for name in selected if name not in choices]
    if unknown:
        raise ValueError(
            f"未知平台：{', '.join(unknown)}。可选平台：{', '.join(available_platforms())}"
        )

    deduped: list[str] = []
    for name in selected:
        if name not in deduped:
            deduped.append(name)
    return deduped


def _previous_init_platforms(root: str | Path) -> list[str]:
    """复用项目清单中上次有效的平台选择。

    工具卸载后项目清单仍保留，重新安装 CLI 不应悄然扩大接入范围。
    清单缺失或不可读时回退到首次安装行为；调用方始终优先采用显式参数。
    """
    paths = ModusPaths.for_root(root)
    if not paths.manifest_path.is_file():
        return []
    try:
        platforms = Manifest.read(paths).platforms
    except (CorruptArtifactError, OSError, TypeError, ValueError):
        return []
    if not platforms:
        return []
    return _validate_selected_platforms(platforms)


# 显式选项优先，交互选择次之，非交互时才考虑历史安装。
# 历史平台只用于无显式参数的恢复路径，不能扩大用户选择。
# 首次非交互安装采用全平台默认时必须返回说明。
def _resolve_init_platforms(
    platforms: list[str] | None,
    all_platforms: bool,
    interactive: bool | None = None,
    prompt: Callable[[list[PlatformPromptChoice]], list[str] | None] | None = None,
    previous_platforms: list[str] | None = None,
) -> InitPlatformSelection:
    if platforms is not None and all_platforms:
        raise ValueError("--platform 与 --all-platforms 不能同时使用")

    if all_platforms:
        return InitPlatformSelection(_ordered_available_platforms())

    if platforms is not None:
        return InitPlatformSelection(_validate_selected_platforms(platforms))

    previous = _validate_selected_platforms(previous_platforms) if previous_platforms else []
    interactive_terminal = is_interactive() if interactive is None else interactive
    if interactive_terminal:
        prompt_fn = _prompt_platforms if prompt is None else prompt
        selected = _validate_selected_platforms(prompt_fn(_platform_prompt_choices()))
        return InitPlatformSelection(selected)

    if previous:
        return InitPlatformSelection(previous)

    return InitPlatformSelection(
        _ordered_available_platforms(),
        (
            "警告：非交互模式下未指定平台；"
            "默认接入全部受支持的平台。"
            "可通过 --platform <名称> 或 --all-platforms 明确指定。"
        ),
    )


# 显式成员是用户确认的边界，不混入自动发现的其他仓库。
# 非交互模式必须提供成员，不能默默接受扫描结果。
def _explicit_workspace_members(candidates: list[str], members: list[str]) -> list[str]:
    if members:
        return list(members)
    if not is_interactive():
        raise ValueError("非交互式工作区初始化需要 --workspace 和至少两个 --member 路径")
    if len(candidates) < 2:
        raise ValueError("发现的嵌套 Git 仓库或工作树不足两个")
    return candidates


# 自动发现仅用于提出工作区候选，确认后才能成为安装目标。
# 拒绝确认终止当前接入，避免误退回单仓库写入。
def _confirm_detected_workspace(candidates: list[str]) -> list[str]:
    if not is_interactive():
        raise ValueError(
            "检测到多个嵌套仓库；请使用 --workspace --member <相对路径> 指定每个已确认的成员"
        )
    rendered = "\n".join(f"  - {item}" for item in candidates)
    if not confirm(
        "检测到多个仓库。是否用以下成员初始化工作区？\n" + rendered,
        default=True,
    ):
        raise ValueError("已取消工作区初始化；可在单个成员的根目录中运行 init")
    return candidates


# 当前目录本身是 Git 根时保留单项目语义。
# 只有根外发现多个仓库才提出隐式工作区确认。
def _resolve_workspace_init(
    *,
    workspace_mode: bool,
    members: list[str],
) -> tuple[bool, list[str]]:
    """解析显式指定或交互确认的工作区成员。"""

    candidates = workspace.discover_members(".")
    current = Path.cwd().resolve()
    nested_multi_repo = git_hygiene.git_root(current) != current and len(candidates) >= 2
    if members and not workspace_mode:
        raise ValueError("使用 --member 时必须指定 --workspace")
    if workspace_mode:
        return True, _explicit_workspace_members(candidates, members)
    if nested_multi_repo:
        return True, _confirm_detected_workspace(candidates)
    return False, []


# 先完成成员与平台解析，再委托接入服务写入资产。
# 参数错误、回滚失败和文件系统失败使用不同的说明。
# 成功报告只在整个接入服务返回后输出。
def init(
    platform: list[str] = _INIT_PLATFORM_OPTION,
    all_platforms: bool = typer.Option(False, "--all-platforms", help="接入全部受支持的平台。"),
    workspace_mode: bool = typer.Option(
        False, "--workspace", help="将当前目录初始化为多仓库工作区。"
    ),
    member: list[str] = _INIT_MEMBER_OPTION,
) -> None:
    """安装项目接入资产；--workspace 用于工作区根目录。"""
    try:
        workspace_mode, selected_members = _resolve_workspace_init(
            workspace_mode=workspace_mode,
            members=member,
        )
        previous_platforms = (
            _previous_init_platforms(".") if not platform and not all_platforms else []
        )
        selected = _resolve_init_platforms(
            platform or None,
            all_platforms,
            previous_platforms=previous_platforms,
        )
        if selected.warning:
            typer.secho(selected.warning, fg=typer.colors.YELLOW, err=True)
        summary = (
            init_project.run_workspace_init(".", selected.platforms, selected_members)
            if workspace_mode
            else init_project.run_init(
                ".",
                selected.platforms,
            )
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    except init_project.InitRollbackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    except init_project.InitFailedError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    except OSError as exc:
        typer.secho(
            f"无法在以下目录初始化 Modus： {Path.cwd()}: {exc}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(1) from exc
    typer.echo(init_report.format_init_success(summary))
