"""Immutable internal values shared by capability composition phases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ..states import Resolution

MAX_CAPABILITY_SLICES: Final = 512
RESOLUTION_ORDER: Final = {
    Resolution.EXACT: 0,
    Resolution.CANDIDATE: 1,
    Resolution.HEURISTIC: 2,
    Resolution.UNRESOLVED: 3,
}


# 边只携带遍历所需的目标、关系与精度，不复制完整事实记录。
@dataclass(frozen=True, slots=True)
class Edge:
    target: str
    relation: str
    resolution: Resolution


# 入口身份和触发信号保持分离，同一符号可承载不同的行为解释。
@dataclass(frozen=True, slots=True)
class Seed:
    entry_id: str
    signature: str
    trigger: dict[str, Any]
    resolution: Resolution


# 外部值无法识别时使用调用方指定的保守等级。
# 默认 unresolved 防止未知词汇无意获得更强的证据地位。
def resolution(value: object, fallback: Resolution = Resolution.UNRESOLVED) -> Resolution:
    try:
        return Resolution(str(value))
    except ValueError:
        return fallback


__all__ = ["Edge", "MAX_CAPABILITY_SLICES", "RESOLUTION_ORDER", "Seed", "resolution"]
