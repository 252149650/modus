"""Claude Code projection: Agent Skills, ``CLAUDE.md``, and lifecycle hooks."""

from __future__ import annotations

import yaml

from ..hooks import bootstrap
from ..precedence import MODUS_DISPATCHER_FALLBACK, USER_INVOKED_WORKFLOWS_SENTENCE
from ..skillmd import split_frontmatter
from .base import Command, PlatformAdapter, Skill
from .models import HookFile, InstructionFile, PlatformLayout
from .rendering import render_skill_index

_LAYOUT = PlatformLayout(
    skills_root=".claude/skills",
    owned_file_prefixes=(".claude/skills/",),
    instructions=InstructionFile(path="CLAUDE.md", marker="modus-claude"),
    hook=HookFile(
        path=".claude/settings.json",
        template="hooks/claude-settings.json",
        render_config=bootstrap.claude_hook_config,
    ),
)


class ClaudeAdapter(PlatformAdapter):
    """Add Claude invocation metadata while reusing the shared lifecycle."""

    name = "claude"
    layout = _LAYOUT

    def _post_process_skill_md(
        self,
        content: str,
        *,
        model_invocable: bool = True,
        user_invocable: bool = False,
    ) -> str:
        """合并宿主调用权限，只重建 YAML 头部，保留正文及已有配置。

        使用统一的有界解析器识别顶层键，避免把正文示例或嵌套键当作权限。
        模板校验由 catalog 负责；无法解析的输入在此保持原样，不猜测修复。
        """
        metadata, body = split_frontmatter(content)
        defaults = {
            "user-invocable": user_invocable,
            "disable-model-invocation": not model_invocable,
        }
        if not metadata or defaults.keys() <= metadata.keys():
            return content

        for key, value in defaults.items():
            metadata.setdefault(key, value)
        newline = "\r\n" if content.startswith("---\r\n") else "\n"
        header = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True, line_break=newline)
        return f"---{newline}{header}---{newline}{body}"

    def _render_instructions(
        self,
        skills: list[Skill],
        _commands: list[Command],
    ) -> str:
        heading = f"## Modus\n\n{MODUS_DISPATCHER_FALLBACK} {USER_INVOKED_WORKFLOWS_SENTENCE}:"
        return render_skill_index(skills, heading)
