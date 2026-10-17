"""Layer output contracts: invariants, identities and digest separation."""

from __future__ import annotations

import pytest

from modus.machine.capability import (
    CapabilitySlice,
    PathStatus,
    PathStep,
    RepresentativePath,
    TerminalEffect,
    capability_identity,
)
from modus.machine.capability.contracts import EffectKind
from modus.machine.corpus import (
    CorpusBaseline,
    InputRecord,
    InputRole,
    RepositoryIdentity,
    compose_baseline_digests,
)
from modus.machine.errors import BudgetError, ContractError
from modus.machine.implementation import implementation_digests, layer_implementation_digest
from modus.machine.packet import PacketBudget
from modus.machine.projection import Proposal, ProposalDisposition
from modus.machine.resolution import TraversalBudget, TraversalQuery
from modus.machine.states import Resolution, SelectionOutcome

DIGEST = "0" * 64


# 非 included 输入必须有原因，夹具遵循与生产相同的构造约束。
def _record(path: str, outcome: SelectionOutcome = SelectionOutcome.INCLUDED) -> InputRecord:
    if outcome is SelectionOutcome.INCLUDED:
        return InputRecord(path, InputRole.SOURCE, outcome, content_digest=DIGEST)
    return InputRecord(path, InputRole.SOURCE, outcome, reason_code="machine.corpus.oversized")


# 源码内容身份与分析配置身份独立，便于定位缓存失效原因。
# 排除记录保留审计价值，但不改变真正参与分析的源码摘要。
class TestCorpusBaseline:
    # 缺失原因会使范围裁剪无法审计，必须在值对象构造时拒绝。
    def test_every_non_included_outcome_requires_a_reason(self) -> None:
        with pytest.raises(ContractError) as caught:
            InputRecord("a.py", InputRole.SOURCE, SelectionOutcome.EXCLUDED)
        assert caught.value.code == "machine.corpus.outcome-without-reason"

    # 没有内容摘要的输入无法证明冻结身份，不能作为已纳入证据。
    def test_included_inputs_require_content_digest(self) -> None:
        with pytest.raises(ContractError) as caught:
            InputRecord("a.py", InputRole.SOURCE, SelectionOutcome.INCLUDED)
        assert caught.value.code == "machine.corpus.included-without-digest"

    # 分别只改配置和源码集合，避免一个综合摘要掩盖两轴错误耦合。
    def test_corpus_and_context_digests_are_independent(self) -> None:
        inputs = (_record("a.py"), _record("b.py"))
        base = compose_baseline_digests(inputs, {"compilerMode": "auto"})
        context_changed = compose_baseline_digests(inputs, {"compilerMode": "off"})
        corpus_changed = compose_baseline_digests((_record("a.py"),), {"compilerMode": "auto"})

        assert base.corpus_digest == context_changed.corpus_digest
        assert base.analysis_context_digest != context_changed.analysis_context_digest
        assert base.baseline_digest != context_changed.baseline_digest

        assert base.corpus_digest != corpus_changed.corpus_digest
        assert base.analysis_context_digest == corpus_changed.analysis_context_digest

    # 加入无关被排除文件不应导致已冻结证据重新编号。
    def test_excluded_inputs_do_not_shape_corpus_identity(self) -> None:
        included = (_record("a.py"),)
        with_excluded = (_record("a.py"), _record("noise.log", SelectionOutcome.EXCLUDED))
        assert (
            compose_baseline_digests(included, {}).corpus_digest
            == compose_baseline_digests(with_excluded, {}).corpus_digest
        )

    # 总数守恒防止被跳过文件既不分析也不出现在缺口报告中。
    def test_outcome_accounting_covers_every_input(self) -> None:
        baseline = CorpusBaseline(
            repository=RepositoryIdentity("abc", False),
            inputs=(_record("a.py"), _record("big.bin", SelectionOutcome.OVERSIZED)),
            digests=compose_baseline_digests((_record("a.py"),), {}),
        )
        counts = baseline.outcome_counts()
        assert counts == {"included": 1, "oversized": 1}
        assert sum(counts.values()) == len(baseline.inputs)


# 实现指纹按层演进，结构拆分必须同步登记实际代码所有者。
# 未知层不得回退到某个默认摘要，否则缓存可能错误命中。
class TestImplementationIdentity:
    def test_each_layer_has_an_independent_implementation_digest(self) -> None:
        digests = implementation_digests()
        assert set(digests) == {
            "corpus",
            "facts",
            "resolution",
            "capability",
            "projection",
            "packet",
            "store",
            "runtime",
            "shared",
            "pipeline",
        }
        assert all(len(value) == 64 for value in digests.values())
        assert digests["facts"] == layer_implementation_digest("facts")

    def test_unknown_implementation_layer_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="machine.pipeline.unknown-implementation-layer"):
            layer_implementation_digest("everything")


# 身份来自入口和可观察效果，不依赖遍历位置或效果枚举顺序。
# 行为改变应获得新身份，排列改变则应保持身份。
class TestCapabilityIdentity:
    def test_identity_is_content_derived_and_order_insensitive(self) -> None:
        first = capability_identity("POST /approve", ("Contract",), ("event:a", "write:t"))
        second = capability_identity("POST /approve", ("Contract",), ("write:t", "event:a"))
        assert first == second
        assert first.startswith("cap-")

    def test_identity_changes_with_behaviour_not_position(self) -> None:
        base = capability_identity("POST /approve", ("Contract",), ("write:t",))
        other_entry = capability_identity("POST /reject", ("Contract",), ("write:t",))
        other_effect = capability_identity("POST /approve", ("Contract",), ("write:u",))
        assert len({base, other_entry, other_effect}) == 3


# 能力整体的精度不能强于代表路径中最弱的一步。
# 此处主动构造不一致切片，防止测试只覆盖天然合法输入。
class TestCapabilitySlice:
    def _slice(self, resolution: Resolution) -> CapabilitySlice:
        path = RepresentativePath(
            steps=(PathStep("entity:a"), PathStep("entity:b")),
            status=PathStatus.TERMINATED,
            resolution=Resolution.CANDIDATE,
        )
        effect = TerminalEffect(EffectKind.DATA_WRITE, "table:contract")
        return CapabilitySlice(
            capability_id="cap-x",
            entry_signature="POST /approve",
            trigger={"kind": "http"},
            entry={"symbol": "approve"},
            terminal_effects=(effect,),
            representative_paths=(path,),
            resolution=resolution,
        )

    def test_slice_resolution_respects_the_floor(self) -> None:
        assert self._slice(Resolution.CANDIDATE).resolution_is_consistent()
        assert not self._slice(Resolution.EXACT).resolution_is_consistent()


# 提案必须有行为证据；remainder 单独表示尚未归纳的剩余范围。
# 空余项不是虚构能力，不能套用普通提案的非空规则。
class TestProposal:
    def test_proposal_without_behaviour_evidence_is_rejected(self) -> None:
        with pytest.raises(ContractError) as caught:
            Proposal(proposal_id="p1", kind="module", capability_ids=())
        assert caught.value.code == "machine.projection.proposal-without-capability"

    def test_remainder_may_be_empty(self) -> None:
        proposal = Proposal(
            proposal_id="p2",
            kind="module",
            capability_ids=(),
            disposition=ProposalDisposition.REMAINDER,
        )
        assert proposal.to_dict()["disposition"] == "remainder"


# 预算既约束项目规模，也保证最小协议信封能够编码。
# 过小字节预算和零条目预算都不能产生可用结果。
class TestPacketBudget:
    def test_default_budget_is_bounded(self) -> None:
        budget = PacketBudget()
        assert budget.max_items == 128
        assert budget.max_bytes == 256 * 1024

    def test_degenerate_budgets_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            PacketBudget(max_items=0)
        with pytest.raises(ValueError):
            PacketBudget(max_bytes=10)


# 节点、边、深度、路径各有独立上限，任意一个失效都可能失控。
# 无种子查询不能解释为全图遍历，必须显式拒绝。
class TestTraversalContract:
    @pytest.mark.parametrize(
        "arguments",
        [
            {"max_nodes": 0},
            {"max_edges": 0},
            {"max_depth": 0},
            {"max_paths": 0},
        ],
    )
    def test_every_traversal_bound_must_be_positive(self, arguments: dict[str, int]) -> None:
        with pytest.raises(BudgetError) as caught:
            TraversalBudget(**arguments)
        assert caught.value.code == "machine.resolution.invalid-traversal-budget"

    def test_traversal_without_a_seed_is_rejected(self) -> None:
        with pytest.raises(BudgetError) as caught:
            TraversalQuery(())
        assert caught.value.code == "machine.resolution.traversal-without-seeds"
