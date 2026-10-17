"""Typed access to the canonical scaffold template distribution.

The template tree is source code: its names define the public workflow surface
and its frontmatter feeds every platform adapter. This module keeps filesystem
discovery, UTF-8 decoding, and template validation behind one small boundary so
the installer never starts writing from a partially valid distribution.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from ..platforms import Command, Skill
from ..skillmd import split_frontmatter
from ..surface import command_invocation

TEMPLATES_DIR: Final = Path(__file__).with_name("templates")


class TemplateCatalogError(RuntimeError):
    """Base class for an invalid or unreadable scaffold distribution."""


class TemplateNotFoundError(FileNotFoundError, TemplateCatalogError):
    """A required template is absent from the installed distribution."""


class TemplateFormatError(ValueError, TemplateCatalogError):
    """A template violates the canonical scaffold contract."""


@dataclass(frozen=True, slots=True)
class TemplateCatalog:
    """Load the explicit command and skill surface from one template root.

    ``TemplateCatalog`` deliberately accepts the requested names rather than
    discovering public workflows from directories. Adding a file can never
    accidentally expand the product surface; callers must first add its name to
    :mod:`modus.surface`.
    """

    root: Path = TEMPLATES_DIR

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    def read(self, template_id: str) -> str:
        """Return one UTF-8 template identified by a canonical POSIX path."""

        path = self._path(template_id)
        if not path.is_file():
            raise TemplateNotFoundError(f"必需的 Modus 模板缺失：{template_id}")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TemplateCatalogError(
                f"无法读取 Modus 模板：{template_id!r}，要求 UTF-8 编码：{exc}"
            ) from exc
        if not content.endswith("\n"):
            raise TemplateFormatError(f"Modus 模板必须以换行结束：{template_id}")
        return content

    def load_skills(self, names: Sequence[str]) -> list[Skill]:
        """Load and validate skill bundles in the caller's declared order."""

        self._reject_duplicate_names(names, kind="skill")
        return [self._load_skill(name) for name in names]

    def load_commands(self, names: Sequence[str]) -> list[Command]:
        """Load and validate slash-command documents in declared order."""

        self._reject_duplicate_names(names, kind="command")
        commands: list[Command] = []
        for name in names:
            template_id = f"commands/{name}.md"
            body = self.read(template_id)
            metadata, _ = self._required_frontmatter(template_id, body)
            description = metadata.get("description")
            if not isinstance(description, str) or not description.strip():
                raise TemplateFormatError(f"Modus 命令模板需要非空 description：{template_id}")
            expected_heading = f"# /{command_invocation(name)}"
            if expected_heading not in body.splitlines():
                raise TemplateFormatError(
                    f"Modus 命令模板 {template_id} 缺少标题 {expected_heading!r}"
                )
            commands.append(Command(name=name, body=body))
        return commands

    def _load_skill(self, name: str) -> Skill:
        template_id = f"skills/{name}/SKILL.md"
        body = self.read(template_id)
        metadata, _ = self._required_frontmatter(template_id, body)

        declared_name = metadata.get("name")
        if declared_name != name:
            raise TemplateFormatError(
                f"Modus 技能模板 {template_id} 声明了非预期名称 "
                f"{declared_name!r}; expected {name!r}"
            )
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise TemplateFormatError(f"Modus 技能模板需要非空 description： {template_id}")

        skill_root = self._path(f"skills/{name}")
        support_files = {
            path.relative_to(skill_root).as_posix(): self.read(
                path.relative_to(self.root).as_posix()
            )
            for path in sorted(skill_root.rglob("*"))
            if path.is_file() and path.name != "SKILL.md"
        }
        return Skill(
            name=name,
            description=description,
            body=body,
            support_files=support_files,
        )

    @staticmethod
    def _required_frontmatter(template_id: str, text: str) -> tuple[dict, str]:
        metadata, body = split_frontmatter(text)
        if not metadata:
            raise TemplateFormatError(f"Modus 模板需要有效的 YAML frontmatter：{template_id}")
        return metadata, body

    @staticmethod
    def _reject_duplicate_names(names: Sequence[str], *, kind: str) -> None:
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicates:
            joined = ", ".join(duplicates)
            raise TemplateFormatError(f"duplicate {kind} 模板名称：{joined}")

    def _path(self, template_id: str) -> Path:
        if not isinstance(template_id, str):
            raise TemplateFormatError("模板 ID 必须为字符串")
        relative = PurePosixPath(template_id)
        if (
            not template_id
            or relative.is_absolute()
            or "\\" in template_id
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise TemplateFormatError(f"模板 ID 必须为规范的相对 POSIX 路径：{template_id!r}")
        path = self.root.joinpath(*relative.parts)
        try:
            resolved_root = self.root.resolve()
            resolved_path = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise TemplateCatalogError(f"无法解析 Modus 模板路径：{template_id!r}: {exc}") from exc
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise TemplateFormatError(f"模板路径越出目录根：{template_id!r}") from exc
        return path
