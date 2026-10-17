"""Small, explicit contracts for contextual observations and their processing.

These are analysis receipts, not proofs of program execution. In particular a
singleton dispatch set means one target *if the call executes*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from modus.machine.states import Completeness, ExecutionStatus


# 一个构建单元可以有多个编译变体，variant 与输入摘要共同解释观察环境。
# 对外字段沿用既有 JSON 协议，Python 命名变化不能改变序列化身份。
@dataclass(frozen=True)
class AnalysisContext:
    id: str
    build_unit_id: str
    language: str
    input_digest: str
    provider: str
    variant: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": "analysis-context",
            "id": self.id,
            "buildUnitId": self.build_unit_id,
            "language": self.language,
            "inputDigest": self.input_digest,
            "provider": self.provider,
            "variant": dict(self.variant),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AnalysisContext:
        return cls(
            str(value["id"]),
            str(value["buildUnitId"]),
            str(value["language"]),
            str(value["inputDigest"]),
            str(value["provider"]),
            dict(value.get("variant") or {}),
        )


# 处置记录描述某条观察被如何采用或拒绝，不等同于程序执行结果。
# 保留记录种类、上下文和内容摘要，便于跨生产者审计。
@dataclass(frozen=True)
class ObservationDisposition:
    record_kind: str
    record_id: str
    status: str
    reason_code: str = ""
    context_id: str = ""
    content_digest: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "recordKind": "disposition",
            "subjectKind": self.record_kind,
            "recordId": self.record_id,
            "status": self.status,
            "reasonCode": self.reason_code,
            "contextId": self.context_id,
            "contentDigest": self.content_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ObservationDisposition:
        return cls(
            str(value["subjectKind"]),
            str(value["recordId"]),
            str(value["status"]),
            str(value.get("reasonCode", "")),
            str(value.get("contextId", "")),
            str(value.get("contentDigest", "")),
        )


# 执行状态与证据完整性分开建模：缓存命中不代表更完整的语义观察。
# 构造时统一枚举，错误词汇不能悄悄进入发布产物。
@dataclass(frozen=True)
class CoverageReceipt:
    context_id: str
    build_unit_id: str
    provider: str
    source_file: str
    phase: str
    status: ExecutionStatus
    completeness: Completeness
    reason_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ExecutionStatus(self.status))
        object.__setattr__(self, "completeness", Completeness(self.completeness))

    def to_dict(self) -> dict[str, str]:
        return {
            "recordKind": "coverage-receipt",
            "contextId": self.context_id,
            "buildUnitId": self.build_unit_id,
            "provider": self.provider,
            "sourceFile": self.source_file,
            "phase": self.phase,
            "status": str(self.status),
            "analysisCompleteness": str(self.completeness),
            "reasonCode": self.reason_code,
        }

    # 内容身份排除执行遥测，使缓存命中和重算得到相同语义摘要。
    # 完整覆盖使用统一原因码，避免成功路径的偶然文本改变证据身份。
    def to_semantic_dict(self) -> dict[str, str]:
        """Serialize evidence completeness without cache/execution telemetry."""
        value = self.to_dict()
        value.pop("status")
        if self.completeness is Completeness.COMPLETE:
            value["reasonCode"] = "machine.facts.complete"
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageReceipt:
        return cls(
            str(value["contextId"]),
            str(value["buildUnitId"]),
            str(value["provider"]),
            str(value["sourceFile"]),
            str(value["phase"]),
            ExecutionStatus(str(value["status"])),
            Completeness(str(value["analysisCompleteness"])),
            str(value.get("reasonCode", "")),
        )
