"""State-axis laws: monotone degradation, legal transitions, single vocabulary."""

from __future__ import annotations

import pytest

from modus.machine.errors import ContractError, MachineError, validate_code
from modus.machine.states import (
    Completeness,
    Resolution,
    can_transition,
    completeness_floor,
    is_weaker_or_equal,
    require_transition,
    resolution_floor,
    resolution_from_confidence,
)


# 多来源合并采用最弱证据，防止强结果掩盖尚未解析的关系。
def test_resolution_floor_returns_weakest() -> None:
    assert resolution_floor("exact", "candidate") is Resolution.CANDIDATE
    assert resolution_floor("exact", "heuristic", "exact") is Resolution.HEURISTIC
    assert resolution_floor("unresolved", "exact") is Resolution.UNRESOLVED
    assert resolution_floor("exact") is Resolution.EXACT


# 枚举所有合法强弱组合，保护状态排序这一跨层公共契约。
def test_resolution_monotonicity_law() -> None:
    order = ["exact", "candidate", "heuristic", "unresolved"]
    for strong_index, strong in enumerate(order):
        for weak in order[strong_index:]:
            assert is_weaker_or_equal(weak, strong)
    assert not is_weaker_or_equal("exact", "candidate")


# 完整性与解析精度是独立轴；这里验证前者的保守合并。
def test_completeness_floor() -> None:
    assert completeness_floor("complete", "partial") is Completeness.PARTIAL
    assert completeness_floor("complete", "fallback", "partial") is Completeness.FALLBACK
    assert completeness_floor("unavailable", "complete") is Completeness.UNAVAILABLE


# 已发布与隔离状态都不能跳过验证直接互换。
# 非法边需要稳定错误码，供调用者区分输入损坏和存储流程错误。
def test_artifact_lifecycle_moves_strictly_forward() -> None:
    assert can_transition("building", "sealed")
    assert can_transition("sealed", "verified")
    assert can_transition("sealed", "quarantined")
    assert can_transition("verified", "published")
    for illegal in (
        ("building", "published"),
        ("published", "building"),
        ("quarantined", "published"),
        ("verified", "sealed"),
    ):
        assert not can_transition(*illegal)
    with pytest.raises(ContractError) as caught:
        require_transition("building", "published")
    assert caught.value.code == "machine.store.illegal-artifact-transition"


# 未知外部置信度只能降为候选，不能因词义像“确定”就升级。
# 大小写与第三方词汇在转换边界统一处理。
def test_foreign_confidence_never_strengthens_claims() -> None:
    assert resolution_from_confidence("EXTRACTED") is Resolution.EXACT
    assert resolution_from_confidence("INFERRED") is Resolution.CANDIDATE
    assert resolution_from_confidence("AMBIGUOUS") is Resolution.CANDIDATE
    assert resolution_from_confidence("TOTALLY-SURE") is Resolution.CANDIDATE


# 异常继承保护既有调用者；稳定代码保护机器消费者。
def test_machine_error_is_a_value_error_with_stable_code() -> None:
    error = MachineError("machine.corpus.frozen-input-drift", "example")
    assert isinstance(error, ValueError)
    assert error.code == "machine.corpus.frozen-input-drift"
    assert str(error).startswith("machine.corpus.frozen-input-drift")


# 允许的层与原因命名形成诊断协议，不随展示语言变化。
@pytest.mark.parametrize(
    "code",
    ["machine.corpus.ok", "machine.packet.cursor-invalid", "machine.store.page-oversized"],
)
def test_valid_code_formats(code: str) -> None:
    assert validate_code(code) == code


# 分别覆盖旧前缀、未知层、大小写和缺失字段，避免仅检查分隔符。
@pytest.mark.parametrize(
    "code",
    [
        "init-analysis.cursor-invalid",
        "machine.unknown-layer.reason",
        "machine.corpus.CamelCase",
        "machine.corpus.",
        "corpus.reason",
    ],
)
def test_malformed_codes_are_rejected(code: str) -> None:
    with pytest.raises(ValueError):
        validate_code(code)
