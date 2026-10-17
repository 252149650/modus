"""Shared scope semantics for public project/personal maintenance commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["MaintenanceScope", "ScopeResolution", "requested_scope", "resolve_scope"]


class MaintenanceScope(StrEnum):
    """A user-requested or effectively executable maintenance scope."""

    COMBINED = "combined"
    PROJECT = "project"
    PERSONAL = "personal"


@dataclass(frozen=True, slots=True)
class ScopeResolution:
    """Keep requested intent separate from the scope available at runtime."""

    requested: MaintenanceScope
    effective: MaintenanceScope
    project_available: bool

    @property
    def project_skipped(self) -> bool:
        return (
            self.requested is MaintenanceScope.COMBINED
            and self.effective is MaintenanceScope.PERSONAL
        )

    @property
    def include_project(self) -> bool:
        return self.effective in {MaintenanceScope.COMBINED, MaintenanceScope.PROJECT}

    @property
    def include_personal(self) -> bool:
        return self.effective in {MaintenanceScope.COMBINED, MaintenanceScope.PERSONAL}


# 互斥参数检查由 CLI 完成，这里只将明确意图转换为共享枚举。
def requested_scope(*, person: bool, project: bool) -> MaintenanceScope:
    """Resolve already-validated mutually exclusive CLI scope flags."""

    if person:
        return MaintenanceScope.PERSONAL
    if project:
        return MaintenanceScope.PROJECT
    return MaintenanceScope.COMBINED


# 仅 combined 可因项目缺失退化为 personal，显式 project 请求保持原意。
# 同时保存 requested 和 effective，使报告能解释跳过而非伪称全部完成。
def resolve_scope(
    requested: MaintenanceScope,
    *,
    project_available: bool,
) -> ScopeResolution:
    """Downgrade a combined request when the current directory has no install."""

    effective = (
        MaintenanceScope.PERSONAL
        if requested is MaintenanceScope.COMBINED and not project_available
        else requested
    )
    return ScopeResolution(
        requested=requested,
        effective=effective,
        project_available=project_available,
    )
