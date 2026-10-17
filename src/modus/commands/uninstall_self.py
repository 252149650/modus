"""``modus uninstall --person``: fully remove the Modus CLI tool from this machine.

This is a separate, independent track from the project-scoped ``modus
uninstall`` in :mod:`modus.commands.uninstall` (which only ever touches
``.modus/`` and ``modus/`` inside one project's working tree and never
inspects or changes anything outside it). ``--person`` never touches project
files; it only inspects and changes the distribution proven to provide the
running Modus module and entry point (including alternate distribution names):

1. inventories every owner the CLI could be installed through (``uv tool``,
   ``pipx``, or a plain interpreter environment via ``pip``/``uv pip``);
2. uninstalls through each discovered owning package manager;
3. removes a now-dangling ``modus`` launcher (the well-known
   ``~/.local/bin/modus`` uv-tool shim plus every ``modus`` found on
   ``PATH``) so a stale binary cannot resurface on the next shell;
4. clears the owning distribution's wheel/build residue from the ``uv`` cache so a
   later ``uv tool install``/``uv pip install`` cannot resolve a cached copy
   instead of downloading the requested version.

Dry-run by default (mirrors ``modus uninstall``'s own convention): nothing is
invoked or removed on disk until ``apply=True``. A single failed step (e.g.
one manager refusing to uninstall, or a permission error clearing the cache)
is recorded as a warning and does not abort the remaining steps.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import cast

from .permission_errors import is_permission_error, output_indicates_permission_error
from .self_update import (
    PackageOwnershipError,
    Runner,
    installed_distribution_name,
    require_owned_tool,
)

__all__ = ["LAUNCHER_NAME", "run_self_uninstall", "scan_self_install"]

LAUNCHER_NAME = "modus"

WhichAll = Callable[[str], list[str]]


def _default_which_all(name: str) -> list[str]:
    """POSIX ``which -a``: every executable named ``name`` visible on ``PATH``, in order."""
    seen: list[str] = []
    seen_resolved: set[str] = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            resolved = str(candidate.resolve())
        except OSError:
            continue
        if resolved in seen_resolved:
            continue
        seen_resolved.add(resolved)
        seen.append(str(candidate))
    return seen


def _default_home_launcher() -> Path:
    return Path.home() / ".local" / "bin" / LAUNCHER_NAME


def _uv_tool_owns_package(uv_path: str, runner: Runner, package_name: str) -> bool:
    try:
        proc = runner([uv_path, "tool", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        head = line.strip().split()
        if head and head[0] == package_name:
            return True
    return False


def _pipx_owns_package(pipx_path: str, runner: Runner, package_name: str) -> bool:
    try:
        proc = runner([pipx_path, "list", "--short"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        head = line.strip().split()
        if head and head[0] == package_name:
            return True
    return False


def _pip_distribution_present(
    distribution_finder: Callable[[str], object], package_name: str
) -> bool:
    try:
        distribution_finder(package_name)
    except metadata.PackageNotFoundError:
        return False
    return True


def scan_self_install(
    *,
    uv_finder: Callable[[], str | None] | None = None,
    pipx_finder: Callable[[], str | None] | None = None,
    runner: Runner | None = None,
    which_all: WhichAll | None = None,
    distribution_finder: Callable[[str], object] | None = None,
) -> dict:
    """Read-only inventory of every way the Modus CLI is currently installed here.

    Never invokes an uninstall; safe to call at any time (including for a
    plain status report independent of ``--yes``).
    """
    uv_finder = uv_finder or (lambda: shutil.which("uv"))
    pipx_finder = pipx_finder or (lambda: shutil.which("pipx"))
    runner = runner or subprocess.run
    using_default_launcher_scan = which_all is None
    which_all = which_all or (lambda name: _default_which_all(name))
    distribution_finder = distribution_finder or metadata.distribution
    package_name = None
    ownership_error = ""
    receipt = None
    try:
        package_name = installed_distribution_name()
        receipt = require_owned_tool(package_name)
    except PackageOwnershipError as exc:
        ownership_error = str(exc)

    uv_path = uv_finder()
    pipx_path = pipx_finder()
    if receipt is not None and not uv_path:
        ownership_error = (
            f"Modus 环境由 uv 管理（{receipt}），但 uv 不可用。"
            "请恢复 PATH 中的 uv 后重试；已禁用自动回退到 pip。"
        )

    launchers: list[str] = list(which_all(LAUNCHER_NAME))
    home_launcher = _default_home_launcher()
    if using_default_launcher_scan and (home_launcher.exists() or home_launcher.is_symlink()):
        resolved = str(home_launcher)
        if resolved not in launchers:
            launchers.append(resolved)

    uv_owns = (
        not ownership_error
        and bool(uv_path)
        and _uv_tool_owns_package(cast(str, uv_path), runner, cast(str, package_name))
    )
    if receipt is not None and not ownership_error and not uv_owns:
        ownership_error = (
            f"Modus 环境由 uv 管理（{receipt}），但 `uv tool list` 未确认 {package_name!r}。"
            "请确认 uv 使用原工具目录后重试；已禁用自动回退到 pip。"
        )

    return {
        "packageName": package_name,
        "ownershipError": ownership_error,
        "uvAvailable": bool(uv_path),
        "uvToolOwnsPackage": bool(uv_owns),
        "pipxAvailable": bool(pipx_path),
        "pipxOwnsPackage": not ownership_error
        and bool(pipx_path)
        and _pipx_owns_package(cast(str, pipx_path), runner, cast(str, package_name)),
        "pipDistributionPresent": not ownership_error
        and _pip_distribution_present(distribution_finder, cast(str, package_name)),
        "pythonInterpreter": sys.executable,
        "launchers": launchers,
    }


def _run_step(
    command: Sequence[str],
    *,
    runner: Runner,
    apply: bool,
    timeout: int = 60,
) -> dict:
    """Execute (or, if ``apply`` is false, only describe) one uninstall/cleanup command."""
    step: dict = {"command": list(command), "applied": apply}
    if not apply:
        step["status"] = "planned"
        return step
    try:
        proc = runner(command, capture_output=True, text=True, timeout=timeout)
    # NOCA:broad-except(可注入执行器的所有故障必须形成错误步骤，不能冒充卸载成功)
    except Exception as exc:  # noqa: BLE001 - reported below, never raised to the caller
        if is_permission_error(exc):
            step["status"] = "permission-required"
            step["message"] = str(exc)
        else:
            step["status"] = "error"
            step["message"] = str(exc)
        return step
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if output_indicates_permission_error(detail):
            step["status"] = "permission-required"
        else:
            step["status"] = "error"
        step["message"] = detail[:2000]
        return step
    step["status"] = "ok"
    return step


def run_self_uninstall(
    *,
    apply: bool = False,
    uv_finder: Callable[[], str | None] | None = None,
    pipx_finder: Callable[[], str | None] | None = None,
    runner: Runner | None = None,
    which_all: WhichAll | None = None,
    distribution_finder: Callable[[str], object] | None = None,
) -> dict:
    """Fully remove the Modus CLI tool from this machine (not a project).

    Dry-run by default; pass ``apply=True`` to actually run the uninstall
    commands, remove a dangling ``modus`` launcher, and clear the ``uv``
    cache. Every step is best-effort and independently reported: one
    manager/step failing (most commonly a permission error) is recorded as a
    warning rather than aborting the remaining steps.
    """
    runner = runner or subprocess.run
    uv_finder = uv_finder or (lambda: shutil.which("uv"))
    pipx_finder = pipx_finder or (lambda: shutil.which("pipx"))
    distribution_finder = distribution_finder or metadata.distribution

    inventory = scan_self_install(
        uv_finder=uv_finder,
        pipx_finder=pipx_finder,
        runner=runner,
        which_all=which_all,
        distribution_finder=distribution_finder,
    )
    if inventory.get("ownershipError"):
        return {
            "status": "partial",
            "applied": apply,
            "inventory": inventory,
            "steps": [],
            "launchers": inventory["launchers"],
            "removedLaunchers": [],
            "cache": None,
            "warnings": [inventory["ownershipError"]],
            "message": inventory["ownershipError"],
        }
    package_name = inventory["packageName"]
    # A package manager normally removes its entry points. Only clean links
    # that its successful uninstall actually made dangling; other environments
    # can expose a perfectly usable command with the same name on PATH.
    live_launchers = {path for path in inventory["launchers"] if Path(path).exists()}

    uv_path = uv_finder()
    pipx_path = pipx_finder()

    steps: list[dict] = []
    warnings: list[str] = []
    owner_found = False

    if inventory["uvToolOwnsPackage"] and uv_path:
        owner_found = True
        steps.append(
            {
                "owner": "uv-tool",
                **_run_step(
                    [uv_path, "tool", "uninstall", package_name], runner=runner, apply=apply
                ),
            }
        )
    if inventory["pipxOwnsPackage"] and pipx_path:
        owner_found = True
        steps.append(
            {
                "owner": "pipx",
                **_run_step([pipx_path, "uninstall", package_name], runner=runner, apply=apply),
            }
        )
    # A plain pip/uv-pip managed environment is only relevant when neither a
    # dedicated `uv tool` nor `pipx` environment already owns the
    # distribution, so a `uv tool`/`pipx`-managed install is never
    # double-uninstalled through its own interpreter's pip as well.
    if (
        inventory["pipDistributionPresent"]
        and not inventory["uvToolOwnsPackage"]
        and not inventory["pipxOwnsPackage"]
    ):
        owner_found = True
        steps.append(
            {
                "owner": "pip",
                **_run_step(
                    [sys.executable, "-m", "pip", "uninstall", "-y", package_name],
                    runner=runner,
                    apply=apply,
                ),
            }
        )

    for step in steps:
        if step.get("status") in {"error", "permission-required"}:
            warnings.append(f"{step['owner']} 卸载未正常完成： {step.get('message') or '未知错误'}")

    removed_launchers: list[str] = []
    planned_launchers = list(inventory["launchers"])
    if apply and owner_found and not warnings:
        for launcher in planned_launchers:
            path = Path(launcher)
            try:
                if launcher in live_launchers and path.is_symlink() and not path.exists():
                    path.unlink()
                    removed_launchers.append(launcher)
            except OSError as exc:
                if is_permission_error(exc):
                    warnings.append(f"无法删除启动器 {launcher}：权限不足")
                else:
                    warnings.append(f"无法删除启动器 {launcher}: {exc}")

    cache_step: dict | None = None
    if uv_path:
        cache_step = _run_step(
            [uv_path, "cache", "clean", package_name], runner=runner, apply=apply
        )
        if cache_step.get("status") in {"error", "permission-required"}:
            warnings.append(f"uv 缓存清理未正常完成： {cache_step.get('message') or '未知错误'}")

    if not owner_found:
        status = "noop"
        message = (
            f"{package_name} 未通过 `uv tool list`、`pipx list` 或当前解释器（{sys.executable}）找到；"
            "无需通过包管理器卸载。如仍有 `modus` 启动器残留，请检查 `which -a modus`。"
        )
    elif not apply:
        status = "dry-run"
        message = "预览：将按以下列表卸载 Modus CLI（使用 --yes 重新运行以执行）。"
    elif warnings:
        status = "partial"
        message = "Modus CLI 卸载完成但有警告，请查看警告详情。"
    else:
        status = "uninstalled"
        message = "已从本机卸载 Modus CLI。"

    return {
        "status": status,
        "applied": apply,
        "inventory": inventory,
        "steps": steps,
        "launchers": planned_launchers,
        "removedLaunchers": removed_launchers,
        "cache": cache_step,
        "warnings": warnings,
        "message": message,
    }
