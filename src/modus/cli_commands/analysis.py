"""仅供初始化与重建使用的证据分析入口；JSON 与进度分流输出。"""

from __future__ import annotations

import json
from typing import cast

import typer

from .analysis_display import format_progress
from .common import (
    HELP_CONTEXT_SETTINGS,
    ModusCommand,
    ModusGroup,
    machine_error,
    public_error,
)

init_analysis_app = typer.Typer(
    cls=ModusGroup,
    no_args_is_help=True,
    help="为初始化或重建知识创建临时证据。",
    context_settings=HELP_CONTEXT_SETTINGS,
)


_ANALYSIS_CANDIDATE_OPTION = typer.Option([], "--candidate-id", help="候选 ID，可重复指定。")


_ANALYSIS_CAPABILITY_OPTION = typer.Option([], "--capability-id", help="能力 ID，可重复指定。")


_ANALYSIS_BOUNDARY_OPTION = typer.Option([], "--boundary-id", help="边界 ID，可重复指定。")


_ANALYSIS_PATH_OPTION = typer.Option([], "--path", help="仓库相对源码路径，可重复指定。")


_ANALYSIS_SYMBOL_OPTION = typer.Option([], "--symbol", help="符号，可重复指定。")


_ANALYSIS_RECORD_TYPE_OPTION = typer.Option(
    [], "--record-type", help="记录类型，如 sourceEntry 或 authoringSignal，可重复指定。"
)


_ANALYSIS_EVIDENCE_KIND_OPTION = typer.Option(
    [], "--evidence-kind", help="证据类型，如 external-http 或 transaction，可重复指定。"
)


# 命令层只解析选项与传递进度，分析生命周期由 machine API 管理。
# 进度写 stderr，保证 stdout 的机器回执可以直接解析。
# 编译器模式是封闭集合，拒绝拼写错误而不静默降级。
# 中断映射为标准退出码 130，区别于工具链失败。
# 未知内部错误仅公开稳定错误码，异常链保留详细排查信息。
# 回执中的摘要是后续查询的身份，不由终端层重新计算。
# 普通输出与 JSON 输出共享同一构建结果。
@init_analysis_app.command(cls=ModusCommand, name="build")
def init_analysis_build_cmd(
    root: str = typer.Option(".", "--root", help="待分析仓库的根目录。"),
    compiler: str = typer.Option(
        "auto", "--compiler", help="编译器模式：auto（自动）| required（必需）| off（关闭）。"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的构建回执。"),
) -> None:
    """为当前初始化或重建创建临时证据包。"""
    from ..machine import AnalysisOptions, MachineError, analyze_repository
    from ..machine.store.contracts import CompilerMode

    if compiler not in {"auto", "required", "off"}:
        raise typer.BadParameter("可选值为： auto, required, off", param_hint="--compiler")

    def progress(stage: str) -> None:
        typer.echo(f"初始化分析：{format_progress(stage)}", err=True)

    try:
        receipt = analyze_repository(
            root,
            AnalysisOptions(compiler_mode=cast(CompilerMode, compiler)),
            progress=progress,
        )
    except KeyboardInterrupt as exc:
        raise typer.Exit(130) from exc
    except MachineError as exc:
        raise typer.Exit(machine_error(exc, json_output=json_output)) from exc
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        public_error(
            "machine.pipeline.unexpected-failure"
            if json_output
            else "初始化分析未完成，请检查本地工具链；可继续阅读源码与知识。\n错误码：machine.pipeline.unexpected-failure",
            json_output=json_output,
        )
        raise typer.Exit(3) from exc
    payload = receipt.to_dict()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        typer.echo(f"初始化证据已就绪： {receipt.bundle_digest}")


# 查询必须同时提供证据包和基线摘要，拒绝串用不同构建。
# 所有过滤条件转换为不可变元组后进入公开查询契约。
# 条目数与字节数共同约束页面，不能只用数量推断输出大小。
# 游标由查询层绑定过滤条件，终端不解释其内部位置。
# 分页提示走 stderr，stdout 始终保持单个 JSON 对象。
# 改变页大小不会推进游标，调用方需显式传回下一页身份。
# 终端层不擅自重试无效游标，避免无声重复读取。
@init_analysis_app.command(cls=ModusCommand, name="select")
def init_analysis_select_cmd(
    root: str = typer.Option(".", "--root", help="待分析仓库的根目录。"),
    bundle_digest: str = typer.Option(..., "--bundle-digest", help="本轮构建回执中的证据包摘要。"),
    baseline_digest: str = typer.Option(
        ..., "--baseline-digest", help="本轮源码语料与分析上下文的组合摘要。"
    ),
    view: str = typer.Option(..., "--view", help="scope | services | evidence。"),
    candidate_ids: list[str] = _ANALYSIS_CANDIDATE_OPTION,
    capability_ids: list[str] = _ANALYSIS_CAPABILITY_OPTION,
    boundary_ids: list[str] = _ANALYSIS_BOUNDARY_OPTION,
    paths: list[str] = _ANALYSIS_PATH_OPTION,
    symbols: list[str] = _ANALYSIS_SYMBOL_OPTION,
    record_types: list[str] = _ANALYSIS_RECORD_TYPE_OPTION,
    evidence_kinds: list[str] = _ANALYSIS_EVIDENCE_KIND_OPTION,
    cursor: str | None = typer.Option(
        None,
        "--cursor",
        help="上一页返回的游标；省略时从第一页重新开始，修改页大小也不会续读。",
    ),
    max_bytes: int = typer.Option(256 * 1024, "--max-bytes", help="每页 JSON 的最大字节数。"),
    max_items: int = typer.Option(128, "--max-items", help="每页完整记录的最大数量。"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的筛选结果。"),
) -> None:
    """在共享预算内读取本轮初始化证据。"""

    from ..machine import (
        AnalysisIdentity,
        EvidenceBudget,
        EvidenceQuery,
        MachineError,
        open_analysis,
        query_evidence,
    )
    from ..machine.store.contracts import SelectionView

    if view not in {"scope", "services", "evidence"}:
        raise typer.BadParameter("可选值为： scope, services, evidence", param_hint="--view")
    try:
        bundle = open_analysis(root, AnalysisIdentity(bundle_digest, baseline_digest))
        result = query_evidence(
            bundle,
            EvidenceQuery(
                view=cast(SelectionView, view),
                candidate_ids=tuple(candidate_ids),
                capability_ids=tuple(capability_ids),
                boundary_ids=tuple(boundary_ids),
                paths=tuple(paths),
                symbols=tuple(symbols),
                record_types=tuple(record_types),
                evidence_kinds=tuple(evidence_kinds),
                budget=EvidenceBudget(max_bytes=max_bytes, max_items=max_items),
                cursor=cursor,
            ),
        )
    except MachineError as exc:
        raise typer.Exit(machine_error(exc, json_output=json_output)) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        public_error("machine.packet.unexpected-failure", json_output=json_output)
        raise typer.Exit(3) from exc
    payload = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
    typer.echo(payload)
    if result.next_cursor is not None:
        typer.echo(
            "初始化分析：还有后续页面。不提供"
            "游标会从第一页重新开始；修改"
            "--max-items 不会续读。如需更多细节，请保留筛选条件并追加 "
            f"--cursor '{result.next_cursor}'。",
            err=True,
        )


# 上下文请求按创作意图组装，避免调用者穷尽全部证据分页。
# 作用域与预算分开建模，增大预算不会自动扩大范围。
# 代表路径配额与低置信度配额各自保留，便于暴露证据缺口。
# 未知意图在打开证据包前拒绝，报告为明确的参数错误。
# 上下文仍绑定同一组分析身份，不能混合当前磁盘与旧证据。
# 输出由公开数据模型序列化，字段名不在展示层本地化。
# 已知机器错误保留其稳定分类，意外运行错误使用统一边界码。
# 该入口只读取已有分析，不隐式触发重建。
@init_analysis_app.command(cls=ModusCommand, name="context")
def init_analysis_context_cmd(
    root: str = typer.Option(".", "--root", help="待分析仓库的根目录。"),
    bundle_digest: str = typer.Option(..., "--bundle-digest", help="本轮构建回执中的证据包摘要。"),
    baseline_digest: str = typer.Option(
        ..., "--baseline-digest", help="本轮源码语料与分析上下文的组合摘要。"
    ),
    intent: str = typer.Option(
        "scope-discovery",
        "--intent",
        help=(
            "scope-discovery | module-authoring | constraint-extraction | "
            "service-boundary | reinit-diff | coverage-audit。"
        ),
    ),
    candidate_ids: list[str] = _ANALYSIS_CANDIDATE_OPTION,
    capability_ids: list[str] = _ANALYSIS_CAPABILITY_OPTION,
    boundary_ids: list[str] = _ANALYSIS_BOUNDARY_OPTION,
    paths: list[str] = _ANALYSIS_PATH_OPTION,
    symbols: list[str] = _ANALYSIS_SYMBOL_OPTION,
    max_bytes: int = typer.Option(256 * 1024, "--max-bytes", help="上下文包中 JSON 的最大字节数。"),
    max_items: int = typer.Option(128, "--max-items", help="上下文包中决策项的最大数量。"),
    max_paths: int = typer.Option(3, "--max-paths", help="每项能力的代表路径数量上限。"),
    low_resolution_quota: int = typer.Option(
        16, "--low-resolution-quota", help="低置信度或剩余项目的配额。"
    ),
) -> None:
    """按任务组装创作上下文，无需穷尽分页。"""

    from ..machine import (
        AnalysisIdentity,
        ContextRequest,
        MachineError,
        PacketBudget,
        PacketScope,
        TaskIntent,
        build_authoring_context,
        open_analysis,
    )

    try:
        selected_intent = TaskIntent(intent)
    except ValueError as exc:
        raise typer.BadParameter("未知的创作意图", param_hint="--intent") from exc
    try:
        bundle = open_analysis(root, AnalysisIdentity(bundle_digest, baseline_digest))
        packet = build_authoring_context(
            bundle,
            ContextRequest(
                selected_intent,
                PacketScope(
                    candidate_ids=tuple(candidate_ids),
                    capability_ids=tuple(capability_ids),
                    boundary_ids=tuple(boundary_ids),
                    paths=tuple(paths),
                    symbols=tuple(symbols),
                ),
                PacketBudget(
                    max_bytes=max_bytes,
                    max_items=max_items,
                    max_paths_per_capability=max_paths,
                    low_resolution_quota=low_resolution_quota,
                ),
            ),
        )
    except MachineError as exc:
        raise typer.Exit(machine_error(exc, json_output=True)) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        public_error("machine.packet.unexpected-failure", json_output=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps(packet.to_dict(), ensure_ascii=False, separators=(",", ":")))
