"""Read-only access to the versioned schemas distributed with Modus.

Source checkouts and installed wheels place schemas in different locations.
Callers should not need to know which layout they are running from, and an
untrusted schema name must never become an arbitrary filesystem read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any, Final

import yaml

_PACKAGE_SCHEMA_DIR: Final = Path(__file__).parent / "_bundled" / "schemas"
_SOURCE_SCHEMA_DIR: Final = Path(__file__).resolve().parents[2] / "schemas"


class SchemaResourceError(ValueError):
    """内置模式名称或文档违反资源契约。"""


def _schema_path(name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or PurePath(name).name != name
        or name in {".", ".."}
        or "\\" in name
        or "\x00" in name
    ):
        raise SchemaResourceError(f"模式名称必须为单个规范文件名： {name!r}")
    root = _PACKAGE_SCHEMA_DIR if _PACKAGE_SCHEMA_DIR.is_dir() else _SOURCE_SCHEMA_DIR
    return root / name


def load_schema(name: str) -> dict[str, Any]:
    """Load one UTF-8 YAML schema as a mapping.

    ``SchemaResourceError`` identifies a broken distribution separately from
    downstream validation errors, while preserving ``OSError`` for missing or
    unreadable package data.
    """

    path = _schema_path(name)
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SchemaResourceError(f"内置模式无效：{name!r}: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise SchemaResourceError(f"内置模式 {name!r} 必须包含映射")
    return dict(loaded)


__all__ = ["SchemaResourceError", "load_schema"]
