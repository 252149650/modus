"""CodeBuddy adapter: commands under ``.codebuddy/commands`` + a CODEBUDDY.md
section + a best-effort session-start hook in ``.codebuddy/settings.json``."""

from __future__ import annotations

from ..hooks import bootstrap
from ..precedence import MODUS_DISPATCHER_FALLBACK, USER_INVOKED_WORKFLOWS_SENTENCE
from ..surface import command_invocation
from .base import Command, PlatformAdapter, Skill
from .models import HookFile, InstructionFile, PlatformLayout
from .rendering import render_skill_index

_LAYOUT = PlatformLayout(
    skills_root=".codebuddy/skills",
    commands_root=".codebuddy/commands",
    owned_file_prefixes=(".codebuddy/",),
    instructions=InstructionFile(path="CODEBUDDY.md", marker="modus-codebuddy"),
    hook=HookFile(
        path=".codebuddy/settings.json",
        template="hooks/codebuddy-hooks.json",
        render_config=bootstrap.codebuddy_hook_config,
    ),
)


# 宿主命令名沿用公共工作流映射，文案调整不能改变可调用标识。
# 共享技能列表由统一渲染器维护，此适配器只增加 CodeBuddy 命令入口。
# 布局声明让安装、更新与卸载使用相同的资产所有权边界。
class CodeBuddyAdapter(PlatformAdapter):
    """Add CodeBuddy's native command documents to the shared skill projection."""

    name = "codebuddy"
    layout = _LAYOUT

    def _render_instructions(
        self,
        skills: list[Skill],
        commands: list[Command],
    ) -> str:
        heading = (
            f"## Modus\n\n{MODUS_DISPATCHER_FALLBACK} "
            "`.modus/skills/` 是权威源文件；`.codebuddy/skills/` 是 CodeBuddy 接入副本。"
            f"{USER_INVOKED_WORKFLOWS_SENTENCE}."
        )
        lines = [
            render_skill_index(skills, heading),
            "",
            "显式 Modus 工作流命令（仅在用户指名命令或明确要求相应工作流时运行）：",
            "",
        ]
        lines.extend(f"- `/{command_invocation(command.name)}`" for command in commands)
        return "\n".join(lines)
