"""Manifest-aware publication primitives for scaffold assets.

The orchestrator decides *what* belongs in a project. ``ScaffoldWriter`` owns
the narrower question of *how* each file is published and recorded. Keeping
that boundary explicit makes ownership rules reviewable and prevents a future
template type from quietly inheriting the wrong overwrite policy.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from ..knowledge_reader import BoundKnowledgeReadError, read_bound_regular_text
from ..manifest import Manifest
from ..paths import ModusPaths
from ..platforms.base import is_executable_asset
from ..platforms.models import validate_relative_path


class AssetOwnership(Enum):
    """Overwrite policy for one scaffolded asset."""

    MANAGED = "managed"
    USER_SEED = "user-seed"


@dataclass(frozen=True, slots=True)
class ScaffoldAsset:
    """One destination file and the template identity recorded for it."""

    path: str
    content: str
    template: str
    ownership: AssetOwnership = AssetOwnership.MANAGED

    def __post_init__(self) -> None:
        validate_relative_path(self.path, label="接入资产路径")
        validate_relative_path(self.template, label="接入模板 ID")
        if not isinstance(self.content, str):
            raise TypeError(f"接入资产内容必须为文本：{self.path}")


class ScaffoldWriteError(OSError):
    """A scaffold asset could not be published without violating ownership."""


@dataclass(slots=True)
class ScaffoldWriter:
    """Publish assets under one project root and update one manifest."""

    paths: ModusPaths
    manifest: Manifest

    def publish_all(self, assets: Iterable[ScaffoldAsset]) -> None:
        """Publish an ordered asset collection using each ownership policy."""

        for asset in assets:
            self.publish(asset)

    def publish(self, asset: ScaffoldAsset) -> None:
        """Publish one asset and record the bytes Modus now owns."""

        if asset.ownership is AssetOwnership.USER_SEED and self._retain_existing(asset):
            return
        self._write(asset.path, asset.content)
        self.manifest.add_file(asset.path, asset.content, asset.template)

    def ensure_knowledge_directories(self) -> None:
        """Restore unmanifested knowledge directories that intentionally start empty."""

        self._write(self.paths.relative(self.paths.domain_dir / ".gitkeep"), "")

    def _retain_existing(self, asset: ScaffoldAsset) -> bool:
        target = self.paths.root / asset.path
        if not target.exists() and not target.is_symlink():
            return False
        try:
            content = read_bound_regular_text(self.paths.root, target)
        except (BoundKnowledgeReadError, OSError, UnicodeError) as exc:
            raise ScaffoldWriteError(f"无法保留用户拥有的接入种子：{asset.path!r}: {exc}") from exc
        self.manifest.add_file(asset.path, content, asset.template)
        return True

    def _write(self, relative_path: str, content: str) -> None:
        target = self.paths.root / relative_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
            if is_executable_asset(relative_path):
                mode = os.stat(target).st_mode
                target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as exc:
            raise ScaffoldWriteError(f"无法发布接入资产：{relative_path!r}: {exc}") from exc
