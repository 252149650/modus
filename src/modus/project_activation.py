"""Project-local Modus activation policy.

Platform projections are installed per project. ``modus init`` is the opt-in
boundary; built-in defaults are sufficient and an optional ``.modus/config.yaml``
is read only when a project deliberately keeps advanced overrides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from .artifacts_io import CorruptArtifactError
from .config import ModusConfig
from .paths import ModusPaths

ACTIVATION_SCHEMA_VERSION: Final = 1


def activation_payload(
    *,
    configured: bool,
    initialized: bool,
    reason: str,
    config_path: str,
    **details: object,
) -> dict[str, object]:
    """Describe current project activation without changing installation state."""
    return {
        "schemaVersion": ACTIVATION_SCHEMA_VERSION,
        "configured": configured,
        "initialized": initialized,
        "reason": reason,
        "configPath": config_path,
        **details,
    }


def activation_status(root: str | Path = ".") -> dict[str, object]:
    """Return the cheap, read-only project activation wire.

    Project/framework maintenance belongs to ``modus status`` and is
    deliberately absent here so task routing depends only on activation.
    """
    paths = ModusPaths.for_root(root)
    initialized = paths.manifest_path.is_file()
    config_path = paths.relative(paths.config_path)
    if not initialized:
        return activation_payload(
            configured=False,
            initialized=False,
            reason="not-initialized",
            config_path=config_path,
        )
    if paths.config_path.is_file():
        ModusConfig.read(paths)
    return activation_payload(
        configured=paths.config_path.is_file(),
        initialized=True,
        reason="ready",
        config_path=config_path,
    )


def activation_snapshot(root: str | Path = ".") -> dict[str, object]:
    """Return the full activation wire without letting bad config break startup.

    The context resolver is the task-start probe and must explain invalid
    configuration in structured output instead of failing before the agent can
    report the repair action.
    """
    paths = ModusPaths.for_root(root)
    try:
        snapshot = activation_status(paths.root)
        # A config without the install manifest is still inactive, but validate
        # it so a malformed half-install is reported as invalid configuration
        # instead of crashing later when the resolver reads grounding settings.
        if not snapshot["initialized"] and paths.config_path.is_file():
            ModusConfig.read(paths)
            snapshot = {**snapshot, "configured": True}
        return snapshot
    except (CorruptArtifactError, OSError, TypeError, ValueError) as exc:
        initialized = paths.manifest_path.is_file()
        return activation_payload(
            configured=paths.config_path.is_file(),
            initialized=initialized,
            reason="invalid-config",
            config_path=paths.relative(paths.config_path),
            error=str(exc),
            repairCommand="modus init",
        )


__all__ = [
    "ACTIVATION_SCHEMA_VERSION",
    "activation_payload",
    "activation_snapshot",
    "activation_status",
]
