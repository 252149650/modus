"""Authoritative public workflow surface and invocation policy.

The tuples here are intentionally explicit: adding a template file must never
publish a new command by directory discovery alone.  Platform adapters and
help text derive their public identities from this small contract.
"""

from __future__ import annotations

from typing import Final

# Keep this tuple small and explicit.  Directory discovery must never expand the
# public surface implicitly when an unlisted template is present.
CORE_SKILLS: Final[tuple[str, ...]] = (
    "using-modus",
    "modus-init",
    "modus-reinit",
    "modus-update-knowledge",
    "modus-sync-knowledge",
)

CORE_SKILL_SET: Final = frozenset(CORE_SKILLS)

# Invocation categories are also authoritative here. Platform indexes and
# precedence notices derive their wording from these tuples instead of keeping
# independent hard-coded allowlists.
AUTOMATIC_SKILLS: Final[tuple[str, ...]] = (
    "using-modus",
    "modus-sync-knowledge",
)
INTENT_ROUTED_SKILLS: Final[tuple[str, ...]] = ("modus-update-knowledge",)
MODEL_INVOCABLE_SKILLS: Final = frozenset((*AUTOMATIC_SKILLS, *INTENT_ROUTED_SKILLS))

# The explicit tuple is the complete user-invocable workflow surface;
# update-knowledge is both intent-routed and explicit.
EXPLICIT_SKILLS: Final[tuple[str, ...]] = (
    "modus-init",
    "modus-reinit",
    "modus-update-knowledge",
)

# CodeBuddy needs native Markdown commands; the skill-native platforms expose
# the same explicit entry points from their projected Skill bundles.
EXPLICIT_COMMANDS: Final[tuple[str, ...]] = (
    "init",
    "reinit",
    "update-knowledge",
)


def command_invocation(name: str) -> str:
    """将平台中立的命令名称映射到公开斜杠命令标识。"""
    return f"modus-{name}"


__all__ = [
    "AUTOMATIC_SKILLS",
    "CORE_SKILLS",
    "CORE_SKILL_SET",
    "EXPLICIT_COMMANDS",
    "EXPLICIT_SKILLS",
    "INTENT_ROUTED_SKILLS",
    "MODEL_INVOCABLE_SKILLS",
    "command_invocation",
]
