"""项目与个人 CLI 卸载入口；展示范围并保护默认保留的长期知识。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import typer

from ..artifacts_io import CorruptArtifactError
from ..commands import (
    uninstall,
    uninstall_report,
    uninstall_self,
)
from ..commands.maintenance_scope import (
    MaintenanceScope,
    ScopeResolution,
    resolve_scope,
)
from .common import (
    JsonObject,
    is_interactive,
    public_error,
)
from .presentation import confirm


# 个人安装可能不存在，空状态与已卸载状态分别展示。
# 即使没有可卸载项也保留警告，避免隐藏探测失败。
def _print_personal_uninstall_result(cli_result: JsonObject, *, apply: bool) -> None:
    report = uninstall_report.format_personal_report(cli_result, apply=apply)
    if report.empty_all_note:
        typer.secho(report.empty_all_note, fg=typer.colors.YELLOW)
    else:
        typer.echo(report.header)
        if report.tree:
            typer.echo(report.tree)
    for warning in report.warnings:
        typer.secho(f"[警告] {warning}", fg=typer.colors.YELLOW, err=True)


# apply 显式区分预览与执行，输出开关只影响展示。
# JSON 模式禁用人类报告，仍返回完整结构化结果。
def _run_personal_uninstall(*, apply: bool, emit: bool = True) -> JsonObject:
    cli_result = uninstall_self.run_self_uninstall(apply=apply)
    if emit:
        _print_personal_uninstall_result(cli_result, apply=apply)
    return cli_result


# 未接入项目不展示删除树，避免把空计划理解为成功删除。
# 需人工复核的路径单独列出，明确用户内容被保留。
def _print_project_uninstall_result(result: JsonObject) -> None:
    if result["status"] == "not-installed":
        typer.secho(
            "当前项目未接入 Modus，未修改项目文件。",
            fg=typer.colors.YELLOW,
        )
        for warning in result["warnings"]:
            typer.secho(warning, fg=typer.colors.YELLOW)
        return

    typer.echo(uninstall_report.format_project_header(result))
    tree = uninstall_report.format_project_tree(result)
    if tree:
        typer.echo(tree)
    for item in result.get("manualReview") or []:
        typer.secho(f"[保留用户修改] {item}", fg=typer.colors.YELLOW)

    for warning in result.get("warnings") or []:
        typer.secho(f"[警告] {warning}", fg=typer.colors.YELLOW, err=True)


# 同时解释请求范围、项目跳过和知识处置。
# 强调行由报告模型指定，终端不通过字符串匹配寻找警告。
def _uninstall_scope_notice(
    *,
    resolution: ScopeResolution,
    project_result: JsonObject | None,
) -> None:
    lines, underline_at = uninstall_report.scope_notice_lines(
        resolution.requested.value,
        project_skipped=resolution.project_skipped,
        project_result=project_result,
    )
    typer.echo("")
    typer.secho(uninstall_report.NOTICE_SEPARATOR, fg=typer.colors.YELLOW, bold=True)
    for index, line in enumerate(lines):
        typer.secho(
            line,
            fg=typer.colors.YELLOW,
            bold=True,
            underline=index == underline_at,
        )
    typer.secho(uninstall_report.NOTICE_SEPARATOR, fg=typer.colors.YELLOW, bold=True)


# 确认默认拒绝，取消后直接终止执行路径。
# 范围标签对应已解析计划，不重新依据命令行推测目标。
# 取消通过终止控制流保证后续删除不会继续执行。
def _confirm_uninstall_or_abort(*, scope: str) -> None:
    label = {
        "combined": "项目与个人 CLI",
        "project": "项目",
        "personal": "个人 CLI",
    }[scope]
    confirmed = confirm(
        f"确认卸载{label}？将删除以上列出的项目",
        default=False,
    )
    if not confirmed:
        typer.secho("已取消，未执行卸载。", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


# 项目与个人结果分层返回，防止两种卸载状态互相覆盖。
# 请求范围与有效范围同时保留，便于解释组合操作的跳过部分。
# 预览与执行使用一致机器协议，调用方通过结果状态判断是否发生修改。
def _uninstall_json_payload(
    *,
    scope: str,
    effective_scope: str,
    applied: bool,
    project_result: JsonObject | None,
    personal_results: JsonObject | None,
) -> dict[str, object]:
    return {
        "scope": scope,
        "effectiveScope": effective_scope,
        "applied": applied,
        "project": project_result,
        "personal": ({"cli": personal_results} if personal_results is not None else None),
    }


# 机器协议与警告分流，stdout 不夹带自然语言诊断。
# 警告走独立展示通道，保持 JSON 结果可被程序解析。
def _emit_uninstall_json_warnings(
    project_result: JsonObject | None,
    personal_results: JsonObject | None,
) -> None:
    results = [project_result, personal_results]
    for result in results:
        if not result:
            continue
        for warning in result.get("warnings") or []:
            typer.echo(str(warning), err=True)


# 所有互斥项在探测与预览之前拒绝。
# 知识删除只属于项目范围，不能用于纯个人卸载。
# 机器模式必须指定 dry-run 或 yes，不能等待交互输入。
# 兼容保留选项仍接受，但不能与明确删除选项并存。
# 预览与自动确认互斥，防止一条请求同时表达只读和执行意图。
def _validate_uninstall_cli_flags(
    *,
    delete_knowledge: bool,
    keep_knowledge: bool,
    project: bool,
    person: bool,
    all_scopes: bool,
    yes: bool,
    dry_run: bool,
    json_output: bool,
) -> None:
    if sum((project, person, all_scopes)) > 1:
        public_error(
            "--project、--person、--all 只能选择一个。",
            json_output=json_output,
        )
        raise typer.Exit(2)
    if delete_knowledge and keep_knowledge:
        public_error(
            "--delete-knowledge 与兼容选项 --keep-knowledge 不能同时使用。",
            json_output=json_output,
        )
        raise typer.Exit(2)
    if dry_run and yes:
        public_error("--dry-run 与 --yes 不能同时使用。", json_output=json_output)
        raise typer.Exit(2)
    if json_output and not (dry_run or yes):
        typer.echo("--json 需同时使用 --dry-run 或 --yes。", err=True)
        raise typer.Exit(2)
    if person and (delete_knowledge or keep_knowledge):
        public_error(
            "知识选项需配合 --project 或 --all 使用，不能用于 --person。",
            json_output=json_output,
        )
        raise typer.Exit(2)


# 默认与历史 --all 别名共享组合范围，保证旧调用继续可用。
# 显式项目或个人范围不能因另一侧存在资产而扩大。
# 默认双范围策略集中在此处，具体执行仍受已解析的安装可用性约束。
def _requested_uninstall_scope(
    *, project: bool, person: bool, all_scopes: bool
) -> MaintenanceScope:
    """Resolve uninstall scope; the default and ``--all`` both target both levels."""

    if all_scopes:
        return MaintenanceScope.COMBINED
    if person:
        return MaintenanceScope.PERSONAL
    if project:
        return MaintenanceScope.PROJECT
    return MaintenanceScope.COMBINED


# 预览和执行共享同一份项目删除计划，避免确认后重新扩大范围。
# 长期知识默认保留，只有显式选项改变处置策略。
# 个人与项目执行结果分别保存，便于呈现部分完成。
@dataclass(slots=True)
class _UninstallSession:
    """State machine for previewing and applying a two-scope uninstall."""

    resolution: ScopeResolution
    knowledge: uninstall.KnowledgeDisposition
    include_project: bool
    include_personal: bool
    project_plan: uninstall.ProjectUninstallPlan | None = None
    project_preview: JsonObject | None = None
    personal_preview: JsonObject | None = None

    # 仅个人卸载不读取项目计划，保持两种范围的独立性。
    # 项目可操作性由计划判断，不只看目录是否存在。
    # 个人预览也使用 apply=False，在确认前不得卸载安装包。
    # 先完成全部预览，再展示最终范围与知识策略。
    @classmethod
    def prepare(
        cls,
        *,
        delete_knowledge: bool,
        project: bool,
        person: bool,
        all_scopes: bool,
        json_output: bool,
    ) -> _UninstallSession:
        requested = _requested_uninstall_scope(
            project=project,
            person=person,
            all_scopes=all_scopes,
        )
        knowledge = (
            uninstall.KnowledgeDisposition.DELETE
            if delete_knowledge
            else uninstall.KnowledgeDisposition.RETAIN
        )
        project_plan = (
            uninstall.plan_uninstall(".", knowledge=knowledge)
            if requested is not MaintenanceScope.PERSONAL
            else None
        )
        resolution = resolve_scope(
            requested,
            project_available=bool(project_plan and project_plan.actionable),
        )
        session = cls(
            resolution=resolution,
            knowledge=knowledge,
            include_project=resolution.include_project,
            include_personal=resolution.include_personal,
            project_plan=project_plan,
        )
        if project_plan is not None:
            session.project_preview = project_plan.to_report()
            if not json_output and not resolution.project_skipped:
                _print_project_uninstall_result(session.project_preview)
        if session.include_personal:
            session.personal_preview = _run_personal_uninstall(
                apply=False,
                emit=not json_output,
            )
        if not json_output:
            _uninstall_scope_notice(
                resolution=resolution,
                project_result=session.project_preview,
            )
        return session

    # 项目以可执行计划为准，个人以实际步骤或缓存任务为准。
    # 存在报告对象本身不表示有删除工作。
    def has_work(self) -> bool:
        project_work = bool(
            self.include_project and self.project_plan and self.project_plan.actionable
        )
        personal_work = bool(
            self.include_personal
            and self.personal_preview
            and (self.personal_preview.get("steps") or self.personal_preview.get("cache"))
        )
        return project_work or personal_work

    # 预览协议明确 applied=False，不能被消费者误判为已删除。
    # 两侧警告都保留，方便离线审查完整计划。
    def emit_dry_run(self, *, json_output: bool) -> None:
        if json_output:
            _emit_uninstall_json_warnings(self.project_preview, self.personal_preview)
            typer.echo(
                json.dumps(
                    _uninstall_json_payload(
                        scope=self.resolution.requested.value,
                        effective_scope=self.resolution.effective.value,
                        applied=False,
                        project_result=self.project_preview,
                        personal_results=self.personal_preview,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            typer.secho("仅预览，未删除任何内容。", fg=typer.colors.CYAN)

    # 先执行项目计划，再处理个人安装，避免先删除当前工具入口。
    # 执行阶段继续使用经过确认的项目计划。
    # 总 applied 由实际阶段结果决定，空操作不能宣称发生删除。
    # 个人部分完成保留结果并返回非零退出码。
    def apply_and_emit(self, *, json_output: bool) -> None:
        project_result = self.project_preview
        if self.include_project and self.project_plan and self.project_plan.actionable:
            project_result = uninstall.apply_uninstall(self.project_plan)
        if project_result is not None and self.include_project and not json_output:
            _print_project_uninstall_result(project_result)
        personal_result = (
            _run_personal_uninstall(apply=True, emit=not json_output)
            if self.include_personal
            else None
        )
        if json_output:
            _emit_uninstall_json_warnings(project_result, personal_result)
            typer.echo(
                json.dumps(
                    _uninstall_json_payload(
                        scope=self.resolution.requested.value,
                        effective_scope=self.resolution.effective.value,
                        applied=bool(
                            (project_result or {}).get("applied")
                            or (personal_result or {}).get("applied")
                        ),
                        project_result=project_result,
                        personal_results=personal_result,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        if personal_result and personal_result.get("status") == "partial":
            raise typer.Exit(1)


# 入口只编排生命周期，路径所有权和并发变更检查由计划服务承担。
# 旧 --keep-knowledge 参数仅产生兼容提示，默认即保留知识。
# 清单损坏时停止并提供人工复核路径，不猜测可删除文件。
# 无工作时无需确认，JSON 调用仍获得完整空结果。
# 非交互执行必须显式 yes，避免脚本意外接受破坏性默认值。
# 确认后计划变化会失败，要求重新预览，而不扩大既有授权。
# 最终错误报告保留请求范围和知识处置选择。
# 异常链留给调试，公开输出保持稳定可解析的对象。
def uninstall_cmd(
    delete_knowledge: bool = typer.Option(
        False,
        "--delete-knowledge",
        help="卸载项目时同时删除 modus/knowledge；默认保留。",
    ),
    keep_knowledge: bool = typer.Option(
        False,
        "--keep-knowledge",
        hidden=True,
        help="兼容选项；现在默认保留已创作的知识。",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="仅卸载项目资产，保留个人 CLI。",
    ),
    person: bool = typer.Option(
        False,
        "--person",
        help=(
            "通过 uv tool uninstall / pipx uninstall / pip uninstall 卸载个人 CLI；"
            "清理残留启动器（如 ~/.local/bin/modus）与该安装包的 uv 缓存。不读写项目文件。"
        ),
    ),
    all_scopes: bool = typer.Option(
        False,
        "--all",
        help="默认行为的兼容别名：卸载项目资产与个人 CLI。",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="直接执行卸载，无需确认。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览卸载范围，不执行删除。"),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读的 JSON，需同时使用 --dry-run 或 --yes。",
    ),
) -> None:
    """Uninstall both scopes by default while retaining authored knowledge."""
    _validate_uninstall_cli_flags(
        delete_knowledge=delete_knowledge,
        keep_knowledge=keep_knowledge,
        project=project,
        person=person,
        all_scopes=all_scopes,
        yes=yes,
        dry_run=dry_run,
        json_output=json_output,
    )
    if keep_knowledge:
        typer.echo(
            "[警告] 无需指定 --keep-knowledge：默认保留已创作的知识。",
            err=True,
        )
    try:
        session = _UninstallSession.prepare(
            delete_knowledge=delete_knowledge,
            project=project,
            person=person,
            all_scopes=all_scopes,
            json_output=json_output,
        )
    except (CorruptArtifactError, OSError, TypeError, UnicodeError, ValueError) as exc:
        requested = _requested_uninstall_scope(
            project=project,
            person=person,
            all_scopes=all_scopes,
        )
        message = f"无法读取项目清单，未执行卸载： {exc}"
        if json_output:
            project_result: JsonObject = {
                "status": "error",
                "applied": False,
                "message": message,
                "root": str(Path.cwd()),
                "projectKind": "unknown",
                "removedFiles": [],
                "removedDirs": [],
                "removedSections": [],
                "platforms": [],
                "manualReview": [".modus/manifest.json"],
                "warnings": [],
                "knowledge": {
                    "path": "modus/knowledge",
                    "present": Path("modus/knowledge").exists(),
                    "disposition": "delete" if delete_knowledge else "retain",
                },
            }
            typer.echo(
                json.dumps(
                    _uninstall_json_payload(
                        scope=requested.value,
                        effective_scope=requested.value,
                        applied=False,
                        project_result=project_result,
                        personal_results=None,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            public_error(message, json_output=False)
        raise typer.Exit(1) from exc
    if dry_run:
        session.emit_dry_run(json_output=json_output)
        return
    has_work = session.has_work()
    if not has_work:
        if json_output:
            session.apply_and_emit(json_output=True)
        return
    if not yes:
        if not is_interactive():
            public_error(
                "非交互模式请使用 --dry-run 预览，或 --yes 执行。",
                json_output=False,
            )
            raise typer.Exit(2)
        _confirm_uninstall_or_abort(scope=session.resolution.effective.value)
    try:
        session.apply_and_emit(json_output=json_output)
    except (uninstall.UninstallPlanChangedError, OSError, UnicodeError, ValueError) as exc:
        message = f"卸载未完成： {exc}"
        if json_output:
            failed_project_result: JsonObject | None = session.project_preview
            if failed_project_result is not None:
                failed_project_result = {
                    **failed_project_result,
                    "status": "error",
                    "message": message,
                }
            typer.echo(
                json.dumps(
                    _uninstall_json_payload(
                        scope=session.resolution.requested.value,
                        effective_scope=session.resolution.effective.value,
                        applied=False,
                        project_result=failed_project_result,
                        personal_results=None,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            public_error(message, json_output=False)
        raise typer.Exit(1) from exc
