"""项目与个人 CLI 更新的终端编排；预览、授权范围与结果分别表达。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import typer

from .. import __version__
from ..artifacts_io import CorruptArtifactError
from ..commands import (
    self_update,
    update,
    update_report,
)
from ..commands.install_state import InitRollbackError
from ..commands.maintenance_scope import (
    MaintenanceScope,
    ScopeResolution,
    requested_scope,
    resolve_scope,
)
from ..manifest import Manifest
from ..paths import ModusPaths
from .common import (
    JsonObject,
    is_interactive,
    public_error,
)
from .presentation import confirm


# 空结果仍保留标准变更字段，机器调用方无需为每种跳过状态分支。
# 版本信息描述当前报告，不意味着已执行安装包升级。
def _empty_update_result(status: str, message: str, installed_version: str) -> JsonObject:
    return {
        "status": status,
        "message": message,
        "installedVersion": installed_version,
        "frameworkVersion": __version__,
        "changeKind": "none",
        "appliedFiles": [],
        "addedFiles": [],
        "removedFiles": [],
        "userEditedFiles": [],
        "conflictedFiles": [],
    }


# 兼容显式权限字段与状态值，避免旧报告丢失需要权限的信息。
def _self_permission_required(report: Mapping[str, Any] | None) -> bool:
    return bool(
        report
        and (
            report.get("permissionRequired") is True
            or report.get("status") == "permission-required"
        )
    )


# 个人安装权限不足不阻止已授权的项目本地维护。
# 项目失败状态优先保留，不能被个人阶段的 partial 掩盖。
# 同时保留项目原状态，调用者可分别判断两个阶段。
def _apply_update_permission_partial(
    result: JsonObject,
    *,
    self_update_report: Mapping[str, Any] | None,
    project_status: str | None = None,
) -> JsonObject:
    """Expose permission failures without blocking safe local maintenance."""
    if not _self_permission_required(self_update_report):
        return result

    previous_message = str(result.get("message") or "")
    if project_status is not None:
        result["projectStatus"] = project_status
    if project_status not in {"error", "blocked"}:
        result["status"] = "partial"
    permission_message = str((self_update_report or {}).get("message") or "安装包操作需要权限")
    result["message"] = f"安装包更新需要权限（{permission_message})" + (
        f"；项目维护结果： {previous_message}" if previous_message else ""
    )
    return result


# 平台补充仅服务展示，清单不可读时不猜测平台列表。
# 已有结果中的平台信息优先，避免额外读取覆盖执行时的事实。
def _attach_update_platforms(result: JsonObject, root: str) -> JsonObject:
    if result.get("platforms"):
        return result
    try:
        result["platforms"] = list(Manifest.read(ModusPaths.for_root(root)).platforms or [])
    except (OSError, ValueError, KeyError, CorruptArtifactError):
        result["platforms"] = []
    return result


# 受保护的用户修改与实际更新分开展示。
# 警告走 stderr，便于终端用户与脚本分别处理。
def _print_update_project_result(result: JsonObject, *, apply: bool) -> None:
    typer.echo(update_report.format_project_header(result, apply=apply))
    tree = update_report.format_project_tree(result, apply=apply)
    if tree:
        typer.echo(tree)
    for path in update_report.skipped_project_paths(result):
        typer.secho(f"[保留用户修改] {path}", fg=typer.colors.YELLOW)
    for warning in result.get("warnings") or []:
        typer.secho(f"[警告] {warning}", fg=typer.colors.YELLOW, err=True)


# 个人阶段可能被显式禁用，空报告不产生误导性的完成标题。
def _print_update_person_result(self_update_report: JsonObject, *, apply: bool) -> None:
    report = update_report.format_personal_report(self_update_report, apply=apply)
    if not report.header and not report.empty_note:
        return
    typer.echo(report.header)
    if report.tree:
        typer.echo(report.tree)
    if report.empty_note:
        typer.echo(report.empty_note)


# 显示实际解析后的范围，使确认与即将执行的阶段保持一致。
def _print_update_scope_notice(*, resolution: ScopeResolution) -> None:
    lines, underline_at = update_report.scope_notice_lines(
        resolution.effective.value,
        project_skipped=resolution.project_skipped,
    )
    typer.echo("")
    typer.secho(update_report.NOTICE_SEPARATOR, fg=typer.colors.YELLOW, bold=True)
    for index, line in enumerate(lines):
        typer.secho(
            line,
            fg=typer.colors.YELLOW,
            bold=True,
            underline=index == underline_at,
        )
    typer.secho(update_report.NOTICE_SEPARATOR, fg=typer.colors.YELLOW, bold=True)


# 确认默认拒绝；拒绝后通过退出终止状态机。
# 该函数只在预览完成且确有工作时调用。
def _confirm_update_or_abort(*, scope: str) -> None:
    label = {
        "combined": "项目与个人 CLI",
        "project": "项目",
        "personal": "个人 CLI",
    }[scope]
    confirmed = confirm(
        f"确认更新{label}？将改写以上列出的项目",
        default=False,
    )
    if not confirmed:
        typer.secho("已取消，未执行更新。", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


# 项目结果作为主报告，并嵌入个人阶段的独立结果。
# 请求范围和有效范围同时保留，解释未接入项目时的自动跳过。
# 机器输出汇总两种维护范围，便于调用方分别判断项目与个人安装结果。
def _update_json_payload(
    *,
    scope: str,
    effective_scope: str,
    applied: bool,
    project_result: JsonObject | None,
    self_update_report: JsonObject,
) -> JsonObject:
    if project_result is not None:
        payload = dict(project_result)
    else:
        payload = _empty_update_result(
            str(self_update_report.get("status") or "noop"),
            str(self_update_report.get("message") or ""),
            str(self_update_report.get("toVersion") or __version__),
        )
    payload["selfUpdate"] = self_update_report
    payload["scope"] = scope
    payload["effectiveScope"] = effective_scope
    payload["applied"] = applied
    return payload


# stdout 只输出协议对象，警告不混入 JSON 文本。
# JSON 输出集中在边界，业务服务不依赖终端表现。
def _emit_update_json(payload: JsonObject) -> None:
    for warning in payload.get("warnings") or []:
        typer.echo(str(warning), err=True)
    typer.echo(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


# 明确失败与阻止状态映射非零退出码，其余状态保留既有调用契约。
def _update_exit_for_result(result: Mapping[str, Any] | None) -> None:
    if result is None:
        return
    if result.get("status") in {"error", "blocked"}:
        raise typer.Exit(1)


# 互斥选项在任何预览读取前校验。
# 机器模式必须显式选择预览或执行，不能进入交互确认。
# 互斥参数在预览前拒绝，避免无效请求进入维护阶段。
def _validate_update_cli_flags(
    *,
    project: bool,
    person: bool,
    dry_run: bool,
    yes: bool,
    json_output: bool,
) -> None:
    if person and project:
        public_error(
            "请使用 --person 或 --project；省略两者时同时更新。",
            json_output=json_output,
        )
        raise typer.Exit(2)
    if dry_run and yes:
        public_error("--dry-run 与 --yes 不能同时使用。", json_output=json_output)
        raise typer.Exit(2)
    if json_output and not (dry_run or yes):
        typer.echo("--json 需同时使用 --dry-run 或 --yes。", err=True)
        raise typer.Exit(2)


# 用户指定仅项目时，未接入应报错，不能擅自改为个人升级。
# 显式项目范围没有安装目标时报告失败，不能把空操作表示为更新成功。
def _reject_uninstalled_project_only(
    *,
    root: str,
    scope: str,
    json_output: bool,
) -> None:
    message = f"{root} 尚未接入 Modus，请先运行 `modus init`。"
    if json_output:
        _emit_update_json(
            _update_json_payload(
                scope=scope,
                effective_scope=scope,
                applied=False,
                project_result=_empty_update_result("error", message, __version__),
                self_update_report=self_update.disabled_report(),
            )
        )
    else:
        public_error(message, json_output=False)
    raise typer.Exit(1)


# 会话对象保存预览、范围与个人阶段结果，避免多段流程共享零散状态。
# 项目写入在个人升级之后进行，以便使用新版本模板。
@dataclass(slots=True)
class _UpdateSession:
    """State machine for the preview-confirm-apply CLI update transaction."""

    root: str
    accept_major: bool
    allow_downgrade: bool
    resolution: ScopeResolution
    is_project: bool
    include_project: bool
    include_person: bool
    carried_self_update: JsonObject | None
    project_preview: JsonObject | None
    self_update_report: JsonObject

    # 恢复重启携带的个人报告时，禁止再次执行安装包升级。
    # 项目预览与个人预览都在确认前完成。
    # 范围解析区分请求意图与当前可执行阶段，空项目不是全局失败。
    @classmethod
    def prepare(
        cls,
        *,
        root: str,
        accept_major: bool,
        allow_downgrade: bool,
        person: bool,
        project: bool,
        json_output: bool,
    ) -> _UpdateSession:
        requested = requested_scope(person=person, project=project)
        is_project = update.is_project_root(root)
        carried = self_update.consume_carried_report()
        if carried is not None:
            requested = MaintenanceScope.COMBINED
        if requested is MaintenanceScope.PROJECT and not is_project:
            _reject_uninstalled_project_only(
                root=root,
                scope=requested.value,
                json_output=json_output,
            )
        resolution = resolve_scope(requested, project_available=is_project)
        include_project = resolution.include_project and is_project
        include_person = resolution.include_personal and carried is None
        project_preview = (
            _attach_update_platforms(
                update.preview_update(
                    root,
                    accept_major=accept_major,
                    allow_downgrade=allow_downgrade,
                ),
                root,
            )
            if include_project
            else None
        )
        self_report = (
            carried
            if carried is not None
            else self_update.preview_self_update(accept_major=accept_major)
            if include_person
            else self_update.disabled_report()
        )
        return cls(
            root=root,
            accept_major=accept_major,
            allow_downgrade=allow_downgrade,
            resolution=resolution,
            is_project=is_project,
            include_project=include_project,
            include_person=include_person,
            carried_self_update=carried,
            project_preview=project_preview,
            self_update_report=self_report,
        )

    # 展示不修改计划，返回值只表示确认是否有实际对象。
    # 被阻止的项目不能误显示为“无需更新”。
    def render_preview(self, *, json_output: bool) -> bool:
        """Render the preview and return whether confirmation is meaningful."""

        if not json_output:
            if self.include_project and self.project_preview is not None:
                _print_update_project_result(self.project_preview, apply=False)
            if self.include_person or self.carried_self_update is not None:
                _print_update_person_result(
                    self.self_update_report,
                    apply=self.carried_self_update is not None,
                )
            _print_update_scope_notice(resolution=self.resolution)
        has_work = update_report.project_has_work(self.project_preview) or (
            self.include_person and update_report.personal_has_work(self.self_update_report)
        )
        blocked = (self.project_preview or {}).get("status") in {"error", "blocked"}
        if not has_work and not blocked and not json_output:
            typer.echo("当前无需更新。")
        return has_work

    # 预览完成也需报告计划错误，不能因未写盘就返回成功。
    # JSON 中 applied 明确为 false，便于自动化消费。
    def emit_dry_run(self, *, json_output: bool) -> None:
        """Finish a preview-only transaction with the established exit policy."""

        if json_output:
            _emit_update_json(
                _update_json_payload(
                    scope=self.resolution.requested.value,
                    effective_scope=self.resolution.effective.value,
                    applied=False,
                    project_result=self.project_preview,
                    self_update_report=self.self_update_report,
                )
            )
        else:
            typer.secho("仅预览，未执行更新。", fg=typer.colors.CYAN)
        _update_exit_for_result(self.project_preview)
        if self.self_update_report.get("status") == "error":
            raise typer.Exit(1)

    # 个人升级失败立即终止，避免继续执行含糊的项目阶段。
    # 升级成功后重启当前流程，使项目模板来自新安装包。
    # 携带报告与已确认选项跨重启传递，避免重复升级和重复确认。
    def apply_personal(self, *, json_output: bool) -> None:
        """Apply or re-exec the personal package phase before project writes."""

        if not self.include_person:
            return
        self.self_update_report = self_update.run_self_update(accept_major=self.accept_major)
        if self.self_update_report["status"] == "error":
            payload = _update_json_payload(
                scope=self.resolution.requested.value,
                effective_scope=self.resolution.effective.value,
                applied=False,
                project_result=self.project_preview,
                self_update_report=self.self_update_report,
            )
            payload["status"] = "error"
            payload["message"] = self.self_update_report["message"]
            if json_output:
                _emit_update_json(payload)
            else:
                _print_update_person_result(self.self_update_report, apply=True)
                public_error(str(self.self_update_report["message"]), json_output=False)
            raise typer.Exit(1)
        if self.self_update_report["status"] == "upgraded" and self.include_project:
            self_update.reexec_update(
                self.root,
                self.accept_major,
                self.self_update_report,
                json_output=json_output,
                allow_downgrade=self.allow_downgrade,
            )

    # 项目执行委托领域服务，终端层仅合并两个阶段状态。
    # 个人模式不对项目清单执行更新，即使当前目录已接入。
    def _project_result(self) -> JsonObject:
        if self.include_project:
            result = _attach_update_platforms(
                update.run_update(
                    self.root,
                    accept_major=self.accept_major,
                    allow_downgrade=self.allow_downgrade,
                ),
                self.root,
            )
            return _apply_update_permission_partial(
                result,
                self_update_report=self.self_update_report,
                project_status=result.get("status"),
            )
        if self.is_project:
            return _empty_update_result(
                "self-update-only",
                "已跳过项目技能更新（--person）",
                self.self_update_report.get("toVersion", __version__),
            )
        return self._uninstalled_project_result()

    # 未接入状态与权限不足组合时使用 partial，保留未完成事实。
    # 提示用户接入项目不等于自动执行接入。
    def _uninstalled_project_result(self) -> JsonObject:
        if _self_permission_required(self.self_update_report):
            status = "partial"
            message = f"{self.root} 尚未接入 Modus，且安装包更新需要权限"
        elif self.include_person and self.self_update_report["status"] != "error":
            status = "self-update-only"
            message = (
                f"{self.root} 尚未接入 Modus（缺少 .modus/manifest.json）；"
                f"仅执行 CLI 自身更新（{self.self_update_report['status']}）。"
                "请运行 `modus init` 接入项目。"
            )
        else:
            status = "noop"
            message = (
                f"{self.root} 尚未接入 Modus（缺少 .modus/manifest.json），"
                "且 --project 已跳过 CLI 更新；无需操作。请运行 `modus init` 接入项目，"
                "或使用 --person 升级 Modus CLI。"
            )
        return _empty_update_result(
            status,
            message,
            self.self_update_report.get("toVersion", __version__),
        )

    # 项目写入失败仍报告已完成的个人升级，避免宣称整次操作未发生。
    # 回滚不完整有独立标记，调用方可据此停止后续维护。
    # 最终只输出一份协议对象，再按结果映射退出状态。
    def apply_and_emit(self, *, json_output: bool) -> None:
        """Apply the project phase, emit one final protocol, and map its exit."""

        try:
            project_result = self._project_result()
        except (CorruptArtifactError, OSError, ValueError, InitRollbackError) as exc:
            message = f"项目更新未完成： {exc}"
            failed = _empty_update_result(
                "error", message, str((self.project_preview or {}).get("installedVersion", ""))
            )
            if isinstance(exc, InitRollbackError):
                failed["rollbackIncomplete"] = True
            if json_output:
                _emit_update_json(
                    _update_json_payload(
                        scope=self.resolution.requested.value,
                        effective_scope=self.resolution.effective.value,
                        applied=self.self_update_report.get("status") == "upgraded",
                        project_result=failed,
                        self_update_report=self.self_update_report,
                    )
                )
            else:
                public_error(message, json_output=False)
            raise typer.Exit(1) from exc
        payload = _update_json_payload(
            scope=self.resolution.requested.value,
            effective_scope=self.resolution.effective.value,
            applied=True,
            project_result=project_result,
            self_update_report=self.self_update_report,
        )
        if json_output:
            _emit_update_json(payload)
        else:
            if self.include_project:
                _print_update_project_result(project_result, apply=True)
            if self.include_person or self.carried_self_update is not None:
                _print_update_person_result(self.self_update_report, apply=True)
        _update_exit_for_result(payload)


# 参数、预览、确认、个人升级、项目更新按固定顺序推进。
# 非交互环境缺少明确执行选项时停止，不从 stdin 猜测授权。
# 清单读取失败发生在写入前，以结构化错误返回而不尝试修复清单。
def update_project(
    root: str = typer.Option(".", help="项目根目录。"),
    accept_major: bool = typer.Option(
        True,
        "--accept-major/--compatible-only",
        help=("升级到最新版本，包括主版本更新。--compatible-only 仅升级当前主版本范围内的版本。"),
    ),
    allow_downgrade: bool = typer.Option(
        False,
        "--allow-downgrade",
        help="允许将项目降级到旧版本，默认拒绝降级。",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="仅更新项目技能。",
    ),
    person: bool = typer.Option(
        False,
        "--person",
        help="仅升级个人 CLI。",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="直接执行更新，无需确认。"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="预览变更，不写入文件或升级安装包。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读的 JSON，需同时使用 --dry-run 或 --yes。",
    ),
) -> None:
    """Update the personal CLI and reconcile project assets.

    Preview the affected files before confirmation. Use --dry-run to preview
    without applying changes, or --yes to apply without prompting.
    """
    _validate_update_cli_flags(
        project=project,
        person=person,
        dry_run=dry_run,
        yes=yes,
        json_output=json_output,
    )
    try:
        session = _UpdateSession.prepare(
            root=root,
            accept_major=accept_major,
            allow_downgrade=allow_downgrade,
            person=person,
            project=project,
            json_output=json_output,
        )
    except (CorruptArtifactError, OSError, TypeError, UnicodeError, ValueError) as exc:
        requested = requested_scope(person=person, project=project)
        message = f"无法读取项目清单，未执行更新： {exc}"
        if json_output:
            _emit_update_json(
                _update_json_payload(
                    scope=requested.value,
                    effective_scope=requested.value,
                    applied=False,
                    project_result=_empty_update_result("error", message, __version__),
                    self_update_report=self_update.disabled_report(),
                )
            )
        else:
            public_error(message, json_output=False)
        raise typer.Exit(1) from exc
    has_work = session.render_preview(json_output=json_output)
    if dry_run:
        session.emit_dry_run(json_output=json_output)
        return
    if not yes and has_work:
        if not is_interactive():
            public_error(
                "非交互模式请使用 --dry-run 预览，或 --yes 执行。",
                json_output=False,
            )
            raise typer.Exit(2)
        _confirm_update_or_abort(scope=session.resolution.effective.value)
    session.apply_personal(json_output=json_output)
    session.apply_and_emit(json_output=json_output)
