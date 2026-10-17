"""Self-update support for ``modus update``.

This module intentionally keeps Python package upgrades separate from the
manifest-based project asset update engine. ``modus update`` orchestrates the two
steps, but project files are still reconciled by :mod:`modus.commands.update`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence  # NOCA:wrong-import-order(Py3.11 stdlib)
from importlib import metadata  # NOCA:wrong-import-order(Py3.11 stdlib)
from pathlib import Path  # NOCA:wrong-import-order(Py3.11 stdlib)
from typing import cast  # NOCA:wrong-import-order(Py3.11 stdlib)
from urllib.parse import unquote, urlparse  # NOCA:wrong-import-order(Py3.11 stdlib)

from .. import __version__
from ..versioning import Version
from .permission_errors import is_permission_error, output_indicates_permission_error

__all__ = [
    "ENV_REPORT",
    "PACKAGE_NAME",
    "PackageOwnershipError",
    "consume_carried_report",
    "disabled_report",
    "installed_distribution_name",
    "is_editable_install",
    "is_uv_tool_install",
    "preview_self_update",
    "reexec_update",
    "require_owned_tool",
    "run_self_update",
    "upgrade_command",
]

PACKAGE_NAME = "openmodus"
ENV_REPORT = "MODUS_SELF_UPDATE_REPORT"

Runner = Callable[..., subprocess.CompletedProcess[str]]
VersionProbe = Callable[[], str]
EditableChecker = Callable[[], bool]
UvFinder = Callable[[], str | None]
UvToolChecker = Callable[[str | None], bool]


class PackageOwnershipError(ValueError):
    """Package maintenance cannot prove which distribution/environment it owns."""


def _target_distribution(package_name: str, current_version: str, accept_major: bool) -> str:
    if accept_major:
        return package_name
    current = Version.parse(current_version)
    return f"{package_name}<{current.major + 1}.0.0"


def _receipt_package(receipt: Path) -> str | None:
    try:
        value = tomllib.loads(receipt.read_text(encoding="utf-8"))
        tool = value.get("tool", {})
        requirements = tool.get("requirements", []) if isinstance(tool, dict) else []
    except (OSError, TypeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(requirements, list):
        return None
    if requirements:
        primary = requirements[0]
        if isinstance(primary, dict) and isinstance(primary.get("name"), str) and primary["name"]:
            return primary["name"]
    return None


def installed_distribution_name(executable: str | None = None) -> str:
    """Resolve the distribution that owns the running ``modus`` entry point."""
    current = executable is None or Path(executable).absolute() == Path(sys.executable).absolute()
    if current:
        distributions = []
        for name in metadata.packages_distributions().get("modus") or []:
            try:
                distributions.append(metadata.distribution(name))
            except metadata.PackageNotFoundError:
                continue
    else:
        environment = Path(cast(str, executable)).absolute().parent.parent
        locations = [environment / "Lib" / "site-packages"]
        locations.extend(environment.glob("lib/python*/site-packages"))
        distributions = list(metadata.distributions(path=[str(path) for path in locations]))

    names = set()
    for distribution in distributions:
        if not any(
            entry.group == "console_scripts"
            and entry.name == "modus"
            and entry.value.partition(":")[0].startswith("modus.")
            for entry in distribution.entry_points
        ):
            continue
        if not _distribution_contains_modus(distribution, current=current):
            continue
        distribution_name = cast(Mapping[str, str], distribution.metadata).get("Name")
        if distribution_name:
            names.add(distribution_name)
    if len(names) == 1:
        return names.pop()
    raise PackageOwnershipError(
        f"无法唯一确定 {executable or sys.executable} 中 Modus 模块与入口所属的发行包；"
        f"发现：{', '.join(sorted(names)) or '无'}。"
        "请使用此环境的包管理器检查安装；未自动修改安装包。"
    )


def _distribution_contains_modus(distribution: metadata.Distribution, *, current: bool) -> bool:
    module = sys.modules.get("modus")
    running_file = (
        Path(module.__file__).resolve() if current and module and module.__file__ else None
    )
    for file in distribution.files or ():
        if tuple(file.parts[-2:]) != ("modus", "__init__.py"):
            continue
        candidate = Path(cast(os.PathLike[str], distribution.locate_file(file)))
        if candidate.is_file() and (running_file is None or candidate.resolve() == running_file):
            return True
    # Editable wheels record their source root rather than individual modules.
    try:
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        if not isinstance(direct, dict) or not isinstance(direct.get("dir_info", {}), dict):
            return False
        url = urlparse(direct.get("url", ""))
        source = Path(unquote(url.path)).resolve()
        return bool(
            running_file
            and direct.get("dir_info", {}).get("editable")
            and url.scheme == "file"
            and url.netloc in {"", "localhost"}
            and running_file.is_relative_to(source)
        )
    except (OSError, ValueError, TypeError):
        return False


def require_owned_tool(package_name: str, executable: str | None = None) -> Path | None:
    """A uv receipt owns the whole tool, which may only depend on Modus."""
    receipt = uv_tool_receipt(executable)
    if receipt is None:
        return None
    primary = _receipt_package(receipt)
    if primary and _normalized_distribution_name(primary) == _normalized_distribution_name(
        package_name
    ):
        return receipt
    raise PackageOwnershipError(
        f"Modus 发行包 {package_name!r} 位于 uv 工具 {primary or 'unknown'!r} ({receipt}），"
        "但不拥有该工具。请通过宿主工具的原安装配置管理 Modus 依赖，或将其安装为独立工具。"
        "已禁用自动删除或升级宿主工具，以及 pip 回退。"
    )


def pip_upgrade_command(
    *,
    current_version: str = __version__,
    accept_major: bool = False,
    executable: str | None = None,
    package_name: str | None = None,
) -> list[str]:
    """Build the pip command using pip's native index configuration."""
    package = _target_distribution(package_name or PACKAGE_NAME, current_version, accept_major)
    return [
        executable or sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        package,
    ]


def _normalized_distribution_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def uv_tool_receipt(executable: str | None = None) -> Path | None:
    """Return the receipt for the uv tool environment owning ``executable``.

    A uv-managed tool interpreter lives at ``<tool>/bin/python`` on POSIX and
    ``<tool>/Scripts/python.exe`` on Windows; uv keeps the install options used
    for that environment in ``<tool>/uv-receipt.toml``.  In particular, the
    receipt retains the original package index, which ``uv tool upgrade`` will
    reuse.  Merely running ``uv pip install --python ...`` bypasses that owner
    metadata and can resolve a same-named distribution from another index.
    """
    interpreter = Path(executable or sys.executable).absolute()
    # Do not resolve the interpreter before checking its lexical environment:
    # uv normally symlinks ``<tool>/bin/python`` to a shared managed Python.
    # Resolving first would therefore leave the tool directory and lose the
    # adjacent receipt that owns this environment.
    candidates = [interpreter.parent.parent]
    try:
        resolved_environment = interpreter.resolve().parent.parent
    except OSError:
        resolved_environment = None
    if resolved_environment is not None and resolved_environment not in candidates:
        candidates.append(resolved_environment)

    for environment in candidates:
        receipt = environment / "uv-receipt.toml"
        if not receipt.is_file():
            continue
        # Even an invalid/foreign receipt marks a tool-managed environment.
        # Do not misclassify it as plain pip and mutate it through a fallback.
        return receipt
    return None


def is_uv_tool_install(executable: str | None = None) -> bool:
    """Whether the running Modus interpreter is owned by ``uv tool``."""
    return uv_tool_receipt(executable) is not None


def uv_tool_upgrade_command(
    uv_path: str,
    *,
    current_version: str = __version__,
    accept_major: bool = False,
    package_name: str | None = None,
) -> list[str]:
    """Build the owner-aware command for a uv-tool installation.

    ``uv tool upgrade`` retains the settings from the original install receipt,
    including ``--index-url``.  This is both safer and more correct than
    mutating uv's tool environment through ``uv pip install --python``.
    """
    package = _target_distribution(package_name or PACKAGE_NAME, current_version, accept_major)
    return [
        uv_path,
        "tool",
        "upgrade",
        package,
    ]


def upgrade_command(
    *,
    current_version: str = __version__,
    accept_major: bool = False,
    executable: str | None = None,
    uv_finder: UvFinder | None = None,
    uv_tool_checker: UvToolChecker | None = None,
) -> tuple[list[str], str]:
    """Build an upgrade command through the package environment's owner.

    A uv-tool environment is upgraded through ``uv tool upgrade`` so uv reuses
    its installation receipt. All other Python installations are upgraded
    through that interpreter's pip, even when a separate ``uv`` executable is
    present on ``PATH``.
    """
    uv_finder = uv_finder or (lambda: shutil.which("uv"))
    uv_tool_checker = uv_tool_checker or is_uv_tool_install
    package_name = installed_distribution_name(executable)
    receipt = require_owned_tool(package_name, executable)
    uv_path = uv_finder()
    if receipt is not None and not uv_path:
        raise PackageOwnershipError(
            f"Modus 环境由 uv 管理（{receipt}），但 uv 不可用。"
            "请恢复 PATH 中的 uv 后重试；已禁用自动回退到 pip。"
        )
    if uv_path and (receipt is not None or uv_tool_checker(executable)):
        return (
            uv_tool_upgrade_command(
                uv_path,
                current_version=current_version,
                accept_major=accept_major,
                package_name=package_name,
            ),
            "uv-tool",
        )
    return (
        pip_upgrade_command(
            current_version=current_version,
            accept_major=accept_major,
            executable=executable,
            package_name=package_name,
        ),
        "pip",
    )


def is_editable_install(package_name: str | None = None) -> bool:
    """Return true when the installed distribution is an editable install."""
    try:
        dist = metadata.distribution(package_name or installed_distribution_name())
    except metadata.PackageNotFoundError:
        return False

    try:
        direct_url = dist.read_text("direct_url.json")
    except OSError:
        return False
    if not direct_url:
        return False

    try:
        data = json.loads(direct_url)
    except json.JSONDecodeError:
        return False
    return bool(data.get("dir_info", {}).get("editable"))


# 使用新解释器进程读取安装结果，避免当前进程模块缓存仍返回旧版本。
def probe_installed_version() -> str:
    """Read ``modus.__version__`` in a fresh interpreter after pip finishes."""
    # NOCA:dangerous-subprocess-use-audit(当前解释器执行固定版本查询代码，参数数组不经过 shell)
    proc = subprocess.run(
        [sys.executable, "-c", "import modus; print(modus.__version__)"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        detail = _clip(proc.stderr or proc.stdout)
        raise RuntimeError(f"无法读取升级后的 Modus 版本： {detail}")
    return proc.stdout.strip().splitlines()[-1]


def run_self_update(
    *,
    current_version: str = __version__,
    accept_major: bool = False,
    runner: Runner | None = None,
    version_probe: VersionProbe | None = None,
    editable_checker: EditableChecker | None = None,
    uv_finder: UvFinder | None = None,
    uv_tool_checker: UvToolChecker | None = None,
) -> dict:
    """Upgrade the Python package in-place and report what happened.

    Uses the package environment's native owner. In particular, uv-tool installs
    are upgraded through ``uv tool upgrade`` so their original index settings
    remain in force. Package-index selection remains delegated to the owner.
    """
    runner = runner or subprocess.run
    version_probe = version_probe or probe_installed_version
    editable_checker = editable_checker or is_editable_install

    try:
        if editable_checker():
            return _report(
                "skipped-editable",
                current_version,
                current_version,
                "检测到可编辑安装，已跳过 Python 包自身更新",
            )
        cmd, tool = upgrade_command(
            current_version=current_version,
            accept_major=accept_major,
            uv_finder=uv_finder,
            uv_tool_checker=uv_tool_checker,
        )
    except PackageOwnershipError as exc:
        return _report("error", current_version, current_version, str(exc))
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=180)
    # NOCA:broad-except(执行器故障统一转为失败或权限报告，保留原始原因)
    except Exception as exc:  # pragma: no cover - exercised through CLI/report shape
        if is_permission_error(exc):
            return _permission_report(
                current_version,
                f"需要权限才能执行 {tool} 安装包升级： {exc}",
                command=cmd,
                tool=tool,
            )
        return _report(
            "error",
            current_version,
            current_version,
            f"{tool} 自身更新未完成： {exc}",
            command=cmd,
            tool=tool,
        )

    if proc.returncode != 0:
        detail = _clip(proc.stderr or proc.stdout)
        if output_indicates_permission_error(detail):
            return _permission_report(
                current_version,
                f"需要权限才能通过以下工具更新 Modus 安装包：{tool}: {detail}",
                command=cmd,
                tool=tool,
            )
        return _report(
            "error",
            current_version,
            current_version,
            f"{tool} 自身更新失败： {detail}",
            command=cmd,
            tool=tool,
        )

    try:
        new_version = version_probe()
    # NOCA:broad-except(升级后的版本探测失败必须返回错误，不能报告更新成功)
    except Exception as exc:
        return _report(
            "error",
            current_version,
            current_version,
            f"{tool} 已执行，但无法核验升级后的版本： {exc}",
            command=cmd,
            tool=tool,
        )

    if new_version == current_version:
        return _report(
            "current",
            current_version,
            new_version,
            "Python 安装包已是允许范围内的最新 Modus 版本",
            command=cmd,
            tool=tool,
        )
    return _report(
        "upgraded",
        current_version,
        new_version,
        "已执行 CLI 自身更新",
        command=cmd,
        tool=tool,
    )


def preview_self_update(
    *,
    current_version: str = __version__,
    accept_major: bool = False,
    uv_finder: UvFinder | None = None,
    uv_tool_checker: UvToolChecker | None = None,
    editable_checker: EditableChecker | None = None,
) -> dict:
    """Read-only preview of :func:`run_self_update`: never runs pip/uv.

    Reports which command *would* run (and which tool), so a `--dry-run`
    caller can show it without actually mutating the installed package.
    """
    editable_checker = editable_checker or is_editable_install
    try:
        if editable_checker():
            return _report(
                "skipped-editable",
                current_version,
                current_version,
                "检测到可编辑安装，将跳过 CLI 自身更新",
            )
        cmd, tool = upgrade_command(
            current_version=current_version,
            accept_major=accept_major,
            uv_finder=uv_finder,
            uv_tool_checker=uv_tool_checker,
        )
    except PackageOwnershipError as exc:
        return _report("error", current_version, current_version, str(exc))
    message = f"预览：将尝试通过以下工具升级：{tool}（未执行命令）"
    return _report("dry-run", current_version, current_version, message, command=cmd, tool=tool)


def consume_carried_report() -> dict | None:
    """Pop a self-update report carried across CLI re-exec."""
    raw = os.environ.pop(ENV_REPORT, None)
    if not raw:
        return None
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return _report(
            "invalid-carried-report",
            __version__,
            __version__,
            "已忽略环境中无效的 CLI 更新报告",
        )
    return report if isinstance(report, dict) else None


def disabled_report() -> dict:
    return _report(
        "disabled",
        __version__,
        __version__,
        "--project 已跳过 Python 包自身更新",
    )


def reexec_update(
    root: str,
    accept_major: bool,
    report: dict,
    *,
    json_output: bool = False,
    allow_downgrade: bool = False,
) -> None:
    """Re-run ``modus update --project --yes`` in the upgraded Python package."""
    carried = dict(report)
    carried["message"] = "已更新 CLI，并使用新框架重新执行更新"
    os.environ[ENV_REPORT] = json.dumps(carried, ensure_ascii=False)
    args = [
        sys.executable,
        "-m",
        "modus",
        "update",
        "--root",
        str(root),
        "--project",
        "--yes",
    ]
    if json_output:
        args.append("--json")
    if not accept_major:
        args.append("--compatible-only")
    if allow_downgrade:
        args.append("--allow-downgrade")
    # NOCA:dangerous-os-exec-audit(仅重启当前解释器和固定 Modus 模块，根目录为独立参数且不经 shell)
    os.execv(sys.executable, args)


def _report(
    status: str,
    from_version: str,
    to_version: str,
    message: str,
    *,
    command: Sequence[str] | None = None,
    tool: str | None = None,
) -> dict:
    report: dict = {
        "status": status,
        "fromVersion": from_version,
        "toVersion": to_version,
        "message": message,
    }
    if command is not None:
        report["command"] = list(command)
    if tool is not None:
        report["tool"] = tool
    return report


def _permission_report(
    current_version: str,
    message: str,
    *,
    command: Sequence[str] | None = None,
    tool: str | None = None,
) -> dict:
    report = _report(
        "permission-required",
        current_version,
        current_version,
        message,
        command=command,
        tool=tool,
    )
    report["permissionRequired"] = True
    report["permissionScope"] = "package-environment"
    return report


def _clip(text: str, limit: int = 2000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
