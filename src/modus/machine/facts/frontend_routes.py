"""Project framework filesystem conventions into explicit route contracts."""

from __future__ import annotations

import re
from pathlib import Path

from modus.machine.corpus.source_snapshot import SourcePath


def filesystem_route_projection(
    framework: str, path: SourcePath, framework_root: Path
) -> tuple[str, str, str, str] | None:
    """Project Next/Nuxt file conventions to (route, method, role, routing_root).

    Returns None when the file is not under a recognized routing root or is a
    framework-internal entry (_app, non-page app files, non-api server files).
    """
    framework_relative = path.relative_to(framework_root)
    parts = list(framework_relative.with_suffix("").parts)
    # Framework routing roots are explicit project conventions. An arbitrary
    # nested folder called `server` or `pages` is not a deployment boundary.
    if parts and parts[0] == "src":
        parts = parts[1:]
    routing_root = (
        parts[0]
        if parts and parts[0] in ({"pages", "app"} if framework == "next" else {"pages", "server"})
        else None
    )
    if (
        framework == "next"
        and routing_root == "pages"
        and path.stem in {"_app", "_document", "_error", "_middleware"}
    ):
        routing_root = None
    if (
        framework == "nuxt"
        and routing_root == "server"
        and (len(parts) < 2 or parts[1] not in {"api", "routes"})
    ):
        routing_root = None
    if not (routing_root and (routing_root != "app" or path.stem in {"page", "route"})):
        return None
    segments = parts[parts.index(routing_root) + 1 :]
    route_method = "ANY"
    if framework == "nuxt" and routing_root == "server":
        if segments and segments[0] == "routes":
            segments.pop(0)
        if segments and re.search(r"\.(get|post|put|patch|delete|head|options)$", segments[-1]):
            segments[-1], route_method = segments[-1].rsplit(".", 1)
            route_method = route_method.upper()
    if segments and segments[-1] in {"page", "route", "index"}:
        segments.pop()
    segments = [
        piece
        for piece in segments
        if not (piece.startswith("(") and piece.endswith(")")) and not piece.startswith("@")
    ]
    segments = [
        re.sub(
            r"^\[\[\.\.\.(.+)\]\]$",
            r"{*\1?}",
            re.sub(r"^\[\.\.\.(.+)\]$", r"{*\1}", re.sub(r"^\[([^].]+)\]$", r"{\1}", piece)),
        )
        for piece in segments
    ]
    route = "/" + "/".join(segments)
    role = (
        "server-handler"
        if (framework == "nuxt" and routing_root == "server")
        or (framework == "next" and routing_root == "app" and path.stem == "route")
        or (framework == "next" and routing_root == "pages" and segments and segments[0] == "api")
        else "page"
    )
    return route, route_method, role, routing_root


__all__ = ["filesystem_route_projection"]
