"""Report installation and optional-knowledge health without gating work.

Preflight is observational: every branch is read-only, and unavailable
enhancements are represented in the report instead of preventing agents from
using current source.

Initialization-analysis bundles are intentionally absent: they are private
inputs to one init/reinit session, not project health or runtime readiness.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from .. import __version__
from ..artifacts_io import CorruptArtifactError
from ..config import ModusConfig, project_config_disposition
from ..paths import ModusPaths
from ..project_activation import activation_snapshot
from ..versioning import bump_kind
from .models import JsonObject

__all__ = ["check", "inspect_project"]


def inspect_project(root: str) -> JsonObject:
    """Collect a stable status snapshot for one project or workspace."""
    from ..knowledge_seeds import inspect_team_standard
    from ..retrieval.index import markdown_files

    paths = ModusPaths.for_root(root)
    activation: JsonObject = activation_snapshot(root)
    workspace_config = _read_workspace_config(paths)
    knowledge_kind = "workspace" if workspace_config is not None else "project"
    result: JsonObject = {
        "status": "ok",
        "root": str(paths.root),
        "projectActivation": activation,
        "frameworkVersion": __version__,
        "initialized": activation["initialized"],
        "projectUpdate": _project_update_status(
            paths,
            initialized=bool(activation["initialized"]),
        ),
        "knowledge": {
            "files": len(markdown_files(paths, knowledge_kind)),
            "teamStandard": inspect_team_standard(paths),
        },
    }
    if workspace_config is not None:
        from ..workspace import inspect_workspace_knowledge

        inspect_workspace = cast(Callable[..., JsonObject], inspect_workspace_knowledge)
        result["workspace"] = inspect_workspace(
            paths.root,
            members=workspace_config.workspace["members"],
        )
    return result


def check(root: str) -> JsonObject:
    """Compatibility entry point for the established preflight command."""
    return inspect_project(root)


def _read_workspace_config(paths: ModusPaths) -> ModusConfig | None:
    """Return only an explicitly declared workspace configuration."""
    if not paths.config_path.is_file():
        return None
    try:
        candidate = ModusConfig.read(paths)
    except (CorruptArtifactError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if candidate.kind == "workspace" else None


def _project_update_status(paths: ModusPaths, *, initialized: bool) -> JsonObject:
    """Compare canonical installed assets with the running CLI version."""
    base: JsonObject = {
        "status": "not-applicable",
        "needsUpdate": False,
        "changeKind": "none",
        "installedVersion": None,
        "frameworkVersion": __version__,
        "versionAuthority": paths.relative(paths.manifest_path),
        "error": None,
    }
    if not initialized:
        return base

    try:
        raw = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("安装清单必须为 JSON 对象")
        installed = str(raw["frameworkVersion"])
        change_kind = bump_kind(installed, __version__)
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
        return {
            **base,
            "status": "unknown",
            "needsUpdate": None,
            "changeKind": "unknown",
            "error": f"invalid 安装清单： {exc}",
        }

    try:
        config_retirement_pending = project_config_disposition(paths) == "retire"
    except (OSError, ValueError):
        config_retirement_pending = False
    needs_update = change_kind != "none" or config_retirement_pending
    if change_kind == "downgrade":
        status = "cli-behind"
    elif needs_update:
        status = "update-required"
    else:
        status = "up-to-date"
    return {
        **base,
        "status": status,
        "needsUpdate": needs_update,
        "changeKind": change_kind,
        "installedVersion": installed,
    }
