"""N6 intent packets are useful, bounded and never pagination obligations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from modus.cli import app
from modus.machine import (
    AnalysisOptions,
    ContextRequest,
    ContractError,
    PacketBudget,
    PacketScope,
    TaskIntent,
    analyze_repository,
    build_authoring_context,
    open_analysis,
)
from modus.paths import ModusPaths


# 用结构解析构造真实 bundle，使上下文测试不依赖本机 Java 工具链。
# 入口调用写操作提供一条可追溯代表路径，而非手工拼装最终回包。
def _bundle(root: Path):
    (root / "OrderController.java").write_text(
        """class OrderController {
 @OpenApiPostMapping(apiName = "Create", apiUrl = "/orders")
 public void create() { persist(); }
 private void persist() { repository.save(order); }
}
""",
        encoding="utf-8",
    )
    receipt = analyze_repository(root, AnalysisOptions(compiler_mode="off"))
    return open_analysis(root, receipt.identity)


# 发现包先提供可调查能力，再提供可直接使用的展开句柄。
# 关闭编译器必须留下降级原因，不能把结构证据提升为编译精度。
# 对实际序列化内容做 schema 验证，覆盖字段名与类型的共同契约。
# 扫描发布字节排除本机路径，保证包可跨工作区分享。
# 上下文包不承担翻页义务，消费者无需追逐游标才能开始创作。
def test_scope_packet_leads_with_capabilities_and_precise_handles(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    packet = build_authoring_context(bundle, ContextRequest(TaskIntent.SCOPE_DISCOVERY))
    payload = packet.to_dict()

    assert payload["schemaVersion"] == 1
    assert payload["bundleDigest"] == bundle.bundle_digest
    assert payload["baselineDigest"] == bundle.baseline_digest
    assert payload["requestedScope"] == {
        "candidateIds": [],
        "capabilityIds": [],
        "boundaryIds": [],
        "paths": [],
        "symbols": [],
    }
    assert payload["proposals"][0]["disposition"] == "proposed"
    assert payload["capabilities"][0]["capabilityId"].startswith("cap-")
    assert payload["capabilities"][0]["evidenceTiers"] == {"T2": 1}
    assert payload["capabilities"][0]["degradationReasons"] == ["machine.facts.compiler-disabled"]
    assert payload["proposals"][0]["coverageReceipts"][0]["completeness"] == "partial"
    assert payload["representativePaths"][0]["sourceSymbol"] == "OrderController.create"
    assert payload["terminalEffects"][0]["kind"] == "data-write"
    assert any(handle["kind"] == "capability" for handle in payload["expansionHandles"])
    assert "nextCursor" not in json.dumps(payload)
    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "schemas"
            / "machine"
            / "authoring-context-packet.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)
    published = ModusPaths.for_root(tmp_path).analysis_bundles_dir / bundle.bundle_digest
    serialized = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(published.rglob("*.json"))
    )
    assert str(tmp_path) not in serialized
    assert str(Path.home()) not in serialized


# 以规范紧凑 JSON 的字节数验证预算，避免只统计内存对象条目。
# 裁剪后的包仍要报告省略与部分覆盖，不能让截断伪装为完整结果。
# 先发现候选再定向创作，保留真实消费者的两阶段调用方式。
def test_packet_budget_trims_content_and_reports_omissions(tmp_path: Path) -> None:
    budget = PacketBudget(max_items=2, max_bytes=4_096, max_paths_per_capability=1)
    bundle = _bundle(tmp_path)
    discovery = build_authoring_context(bundle, ContextRequest(TaskIntent.SCOPE_DISCOVERY))
    candidate_id = str(discovery.proposals[0]["proposalId"])
    packet = build_authoring_context(
        bundle,
        ContextRequest(
            TaskIntent.MODULE_AUTHORING,
            scope=PacketScope(candidate_ids=(candidate_id,)),
            budget=budget,
        ),
    )
    encoded = json.dumps(packet.to_dict(), sort_keys=True, separators=(",", ":")).encode()

    assert len(encoded) <= budget.max_bytes
    assert packet.omitted_counts
    assert packet.coverage_receipts[0].completeness == "partial"
    assert packet.coverage_receipts[0].reason_code == "machine.packet.budget-exhausted"


# 模块创作需要显式范围，缺失选择器不能退化成整个仓库。
def test_module_authoring_rejects_an_unbounded_request() -> None:
    with pytest.raises(ContractError, match="machine.packet.module-scope-required"):
        ContextRequest(TaskIntent.MODULE_AUTHORING)


# 创作信号要携带精度、来源层和源码见证，供读者独立判断可信度。
# 计数回执应对应实际发送内容，不沿用裁剪前的总数。
# 保留请求范围和 bundle 身份，让消费者能追溯本次选择。
# 路径句柄允许按需展开细节，无需把全部证据塞进首包。
def test_module_authoring_packet_keeps_scoped_signals_with_provenance(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    discovery = build_authoring_context(
        bundle,
        ContextRequest(TaskIntent.SCOPE_DISCOVERY),
    )
    candidate_id = str(discovery.proposals[0]["proposalId"])

    packet = build_authoring_context(
        bundle,
        ContextRequest(
            TaskIntent.MODULE_AUTHORING,
            scope=PacketScope(candidate_ids=(candidate_id,)),
        ),
    )
    payload = packet.to_dict()
    proposal = payload["proposals"][0]
    signals = proposal["authoringSignals"]
    kinds = {signal["details"]["evidenceKind"] for signal in signals}

    assert {"external-http", "authoritative-write"} <= kinds
    assert payload["requestedScope"]["candidateIds"] == [candidate_id]
    assert payload["bundleDigest"] == bundle.bundle_digest
    assert payload["representativePaths"]
    assert payload["coverageReceipt"][0]["counts"]["authoringSignals"] == len(signals)
    assert all(signal["resolution"] and signal["sourceLayer"] for signal in signals)
    assert all(signal["witness"]["path"] == "OrderController.java" for signal in signals)
    assert any(handle["kind"] == "path" for handle in payload["expansionHandles"])


# 路径是合法入口，用户无需预先知道内部提案 ID。
# 从路径推导的信号仍须绑定所有者，不能作为无归属文本附加。
def test_module_authoring_can_derive_proposal_from_a_source_path(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    packet = build_authoring_context(
        bundle,
        ContextRequest(
            TaskIntent.MODULE_AUTHORING,
            scope=PacketScope(paths=("OrderController.java",)),
        ),
    )

    assert packet.proposals
    assert packet.proposals[0]["authoringSignals"]
    assert not any(
        ambiguity.get("marker") == "unbound-authoring-signal" for ambiguity in packet.ambiguities
    )


# 发现阶段返回的能力句柄必须能直接传回模块创作接口。
# 此往返保护接口可组合性，避免要求调用者拼装内部映射。
def test_capability_expansion_handle_is_a_direct_module_selector(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    discovery = build_authoring_context(bundle, ContextRequest(TaskIntent.SCOPE_DISCOVERY))
    capability_id = str(discovery.capabilities[0]["capabilityId"])

    packet = build_authoring_context(
        bundle,
        ContextRequest(
            TaskIntent.MODULE_AUTHORING,
            scope=PacketScope(capability_ids=(capability_id,)),
        ),
    )

    assert packet.proposals
    assert packet.proposals[0]["authoringSignals"]
    assert packet.to_dict()["requestedScope"]["capabilityIds"] == [capability_id]


# 从 CLI 读取 JSON 验证适配层没有额外套入旧分页信封。
# 两个摘要共同指定分析身份，避免误用“最新”包的可变状态。
# 成功数据保持 stdout 可解析，供脚本直接消费。
def test_internal_context_cli_returns_packet_without_cursor_protocol(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "internal",
            "init-analysis",
            "context",
            "--root",
            str(tmp_path),
            "--bundle-digest",
            bundle.bundle_digest,
            "--baseline-digest",
            bundle.baseline_digest,
            "--intent",
            "scope-discovery",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recordKind"] == "authoring-context-packet"
    assert payload["intent"] == "scope-discovery"
    assert "nextCursor" not in payload


# 缺少范围属于调用契约错误，退出码应区别于内部执行失败。
# requiredAnyOf 给出可恢复的参数选择，不要求调用者解析中文提示。
# 错误协议保留英文键名，展示层本地化不改变机器字段。
def test_internal_context_cli_reports_unbounded_module_authoring_as_contract_error(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "internal",
            "init-analysis",
            "context",
            "--root",
            str(tmp_path),
            "--bundle-digest",
            bundle.bundle_digest,
            "--baseline-digest",
            bundle.baseline_digest,
            "--intent",
            "module-authoring",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "machine.packet.module-scope-required"
    assert payload["error"]["details"]["requiredAnyOf"] == [
        "candidateIds",
        "capabilityIds",
        "paths",
        "symbols",
    ]
