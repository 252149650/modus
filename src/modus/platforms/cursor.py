"""Cursor adapter: skills under ``.cursor/skills`` + a ``.cursor/rules/modus.mdc``
rule + a session-start hook in ``.cursor/hooks.json``."""

from __future__ import annotations

from ..hooks import bootstrap
from ..precedence import MODUS_DISPATCHER_FALLBACK, USER_INVOKED_WORKFLOWS_SENTENCE
from .base import Command, PlatformAdapter, Skill
from .models import HookFile, InstructionFile, PlatformLayout
from .rendering import render_skill_index

_FRONTMATTER = "---\ndescription: Modus 框架入口\nalwaysApply: true\n---\n\n"
_LAYOUT = PlatformLayout(
    skills_root=".cursor/skills",
    owned_file_prefixes=(".cursor/",),
    instructions=InstructionFile(
        path=".cursor/rules/modus.mdc",
        marker="modus-cursor",
        preamble=_FRONTMATTER,
    ),
    hook=HookFile(
        path=".cursor/hooks.json",
        template="hooks/cursor-hooks.json",
        render_config=bootstrap.cursor_hook_config,
    ),
)


# 布局只描述宿主拥有的路径与标记，文件合并和所有权校验由基类负责。
# 常驻规则只呈现工作流入口，具体技能仍从权威资产投影。
class CursorAdapter(PlatformAdapter):
    """Project Modus into Cursor skills, always-on rules, and lifecycle hooks."""

    name = "cursor"
    layout = _LAYOUT

    def _render_instructions(
        self,
        skills: list[Skill],
        _commands: list[Command],
    ) -> str:
        heading = (
            "# Modus\n\n"
            f"{MODUS_DISPATCHER_FALLBACK} {USER_INVOKED_WORKFLOWS_SENTENCE}.\n"
            "Modus 核心能力："
        )
        return render_skill_index(skills, heading)
