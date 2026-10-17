"""Detect frontend frameworks and source execution boundaries."""

from __future__ import annotations

import json
import re
from pathlib import Path

from modus.machine.corpus.source_snapshot import SourcePath


def detect_framework(manifest_inputs: dict[Path, bytes | None]) -> tuple[str, Path | None]:
    """Return (framework, root) from the nearest package.json naming next/nuxt/react/vue.

    The root tracks the last parseable manifest even when no framework is named,
    matching how nested workspaces resolve relative paths.
    """
    framework = ""
    framework_root: Path | None = None
    for manifest, manifest_raw in manifest_inputs.items():
        if manifest_raw is None:
            continue
        try:
            package = json.loads(manifest_raw)
            dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
            framework = next(
                (name for name in ("next", "nuxt", "react", "vue") if name in dependencies), ""
            )
            framework_root = manifest.parent
            if framework:
                break
        except (ValueError, TypeError, AttributeError):
            continue
    return framework, framework_root


def runtime_boundary(
    path: SourcePath, text: str, framework: str, framework_root: Path | None
) -> str:
    """Classify where this file executes from directives, framework layout, and suffix."""
    runtime = "browser" if re.match(r"\s*[\"\x27]use client[\"\x27]", text) else "unspecified"
    if re.match(r"\s*[\"\x27]use server[\"\x27]", text):
        runtime = "server"
    if framework == "next" and framework_root is not None and runtime == "unspecified":
        framework_parts = path.relative_to(framework_root).parts
        if framework_parts[:1] == ("src",):
            framework_parts = framework_parts[1:]
        if framework_parts[:1] == ("app",):
            runtime = "server-component"
    if (
        framework == "nuxt"
        and framework_root
        and path.relative_to(framework_root).parts[0] == "server"
    ):
        runtime = "server"
    if path.suffix == ".vue":
        runtime = (
            "universal-component" if framework == "nuxt" else "component"
        )  # SSR is not inferred from a .vue extension.
    return runtime


__all__ = ["detect_framework", "runtime_boundary"]
