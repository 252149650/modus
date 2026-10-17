"""Pipeline publication and scope contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from _support.analysis import bundle_path, sample_project
from modus.machine import (
    AnalysisIdentity,
    AnalysisOptions,
    DriftError,
    EvidenceQuery,
    analyze_repository,
    open_analysis,
    query_evidence,
)
from modus.machine.store.bundle import canonical_bytes


# 相同源码连续分析应得到相同包身份和逐文件字节。
# 读取必须显式提供匹配的基线身份，不能只凭目录存在信任产物。
# 同时走一次范围查询，确认确定性包确实可用。
def test_bundle_is_deterministic_and_explicitly_verified(tmp_path: Path) -> None:
    root = sample_project(tmp_path)
    first = analyze_repository(root, AnalysisOptions(compiler_mode="auto"))
    first_path = bundle_path(root, first.bundle_digest)
    # 比较全部 JSON 字节而非只比较摘要，捕获非确定序列化顺序。
    first_files = {
        path.relative_to(first_path).as_posix(): path.read_bytes()
        for path in first_path.rglob("*.json")
    }
    second = analyze_repository(root, AnalysisOptions(compiler_mode="auto"))
    assert second.bundle_digest == first.bundle_digest
    assert {
        path.relative_to(bundle_path(root, second.bundle_digest)).as_posix(): path.read_bytes()
        for path in bundle_path(root, second.bundle_digest).rglob("*.json")
    } == first_files

    bundle = open_analysis(root, first.identity)
    assert query_evidence(bundle, EvidenceQuery(view="scope")).items
    with pytest.raises(ValueError, match="baseline-mismatch"):
        open_analysis(root, AnalysisIdentity(first.bundle_digest, "0" * 64))


# 进度是观察通道，开启它不能改变分析身份。
# 测试固定阶段顺序和最终完成事件，保留消费者的进度协议。
def test_progress_observes_the_six_stages_without_changing_identity(tmp_path: Path) -> None:
    root = sample_project(tmp_path)
    quiet = analyze_repository(root, AnalysisOptions(compiler_mode="off"))
    events: list[str] = []

    observed = analyze_repository(
        root,
        AnalysisOptions(compiler_mode="off"),
        progress=events.append,
    )

    assert observed.identity == quiet.identity
    assert events[:6] == ["freeze", "observe", "resolve", "compose", "project", "package"]
    # 只有六阶段完成后才发布完成事件，不能以进入打包阶段代替成功。
    assert events[-1] == "complete"


# 根清单可通过而叶页已损坏的情况，应在按需查询时被拒绝。
# 这验证惰性认证边界，防止只验证清单就信任所有正文。
# 查询限定源码路径，确保实际访问被篡改的证据目录。
def test_tampered_or_cross_bundle_file_is_rejected(tmp_path: Path) -> None:
    root = sample_project(tmp_path)
    receipt = analyze_repository(root, AnalysisOptions(compiler_mode="off"))
    manifest_path = bundle_path(root, receipt.bundle_digest) / "manifest.json"
    assert set(json.loads(manifest_path.read_text(encoding="utf-8"))["roots"]) == {
        "snapshot",
        "scope",
        "services",
        "evidence",
        "diagnostics",
    }
    # 直接修改已发布叶页，绕过正常写入器以模拟磁盘损坏。
    evidence_path = next(
        path for path in bundle_path(root, receipt.bundle_digest).glob("evidence/_data/*.json")
    )
    evidence_path.write_bytes(canonical_bytes({"schemaVersion": 1, "tampered": True}))
    bundle = open_analysis(root, receipt.identity)
    with pytest.raises(ValueError, match="catalog-digest"):
        query_evidence(bundle, EvidenceQuery(view="evidence", paths=("orders.py",)))


# 在解析与发布之间修改源码，模拟分析期间的真实输入漂移。
# 漂移必须阻止发布，不能留下看似完整的新包。
# 注入只执行一次，避免后续重复修改掩盖首个漂移位置。
def test_source_change_during_pipeline_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modus.machine.pipeline as init_pipeline

    root = sample_project(tmp_path)
    original = init_pipeline.build_evidence_graph
    changed = False

    def mutate_once(extraction: dict, *, root: Path | None = None):
        nonlocal changed
        graph = original(extraction, root=root)
        if root is not None and not changed:
            changed = True
            (root / "orders.py").write_text("# changed during analysis\n", encoding="utf-8")
        return graph

    # 在真实图构建后注入漂移，保持其余管线不变。
    monkeypatch.setattr(init_pipeline, "build_evidence_graph", mutate_once)
    with pytest.raises(DriftError, match="machine.corpus.changed-during-build"):
        analyze_repository(root, AnalysisOptions(compiler_mode="off"))
    # 失败检查同时覆盖目录未创建与空目录两种合法清理结果。
    bundles = root / "modus/.cache/init-analysis/bundles"
    assert not bundles.exists() or not list(bundles.iterdir())


# 固定大 CPU 数量和内存输入，消除测试宿主差异。
# 分别核对结构工作者与编译器工作者上限，避免高并发放大工具资源消耗。
def test_resource_profiles_bound_structural_and_compiler_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modus.machine.runtime.resources import resolve_policy

    monkeypatch.setattr("modus.machine.runtime.resources.os.cpu_count", lambda: 64)
    memory = 16 * 1024**3
    assert resolve_policy("low-memory", memory_budget=memory).workers == 2
    assert resolve_policy("balanced", memory_budget=memory).workers == 8
    assert resolve_policy("auto", memory_budget=memory).workers == 16
    throughput = resolve_policy("throughput", memory_budget=memory)
    assert throughput.workers == 32
    # 编译器并发单独受限，不能照搬结构提取的高吞吐工作者数量。
    assert throughput.compiler_workers == 2


# 刻意将两个构建单元放入同一聚类，验证单元边界优先于聚类结果。
# 测试源码可以计入支持材料，但不能伪装成生产入口。
# 显式 HTTP 契约应优先成为代表入口，避免工具函数占据首屏。
def test_scope_candidates_never_cross_primary_units_and_rank_contracts_first(
    tmp_path: Path,
) -> None:
    import networkx as nx

    from modus.machine.corpus.source_roles import build_unit_memberships, source_role
    from modus.machine.projection.scope_candidates import project_scope_candidates

    for relative, content in (
        ("apps/orders/api.py", "def handle_order():\n    pass\n"),
        ("apps/orders/helper.py", "def _identifier_stem():\n    pass\n"),
        ("apps/billing/api.py", "def charge():\n    pass\n"),
        ("tests/test_orders.py", "def test_order():\n    pass\n"),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
    graph.add_node(
        "orders-api",
        source_file="apps/orders/api.py",
        label="handle_order",
        kind="function",
        source_location="L1",
        contracts=[{"kind": "http", "identity": "POST /orders"}],
    )
    graph.add_node(
        "orders-helper",
        source_file="apps/orders/helper.py",
        label="_identifier_stem",
        kind="function",
        source_location="L1",
    )
    graph.add_node(
        "billing-api",
        source_file="apps/billing/api.py",
        label="charge",
        kind="function",
        source_location="L1",
    )
    graph.add_node(
        "orders-test",
        source_file="tests/test_orders.py",
        label="test_order",
        kind="function",
        source_location="L1",
    )
    source_digests = {
        relative: "a" * 64
        for relative in (
            "apps/orders/api.py",
            "apps/orders/helper.py",
            "apps/billing/api.py",
            "tests/test_orders.py",
        )
    }
    # 构建单元显式声明文件归属，测试不依赖路径启发式碰巧分组正确。
    build_units = [
        {
            "id": "orders",
            "root": "apps/orders",
            "files": [
                "apps/orders/api.py",
                "apps/orders/helper.py",
                "tests/test_orders.py",
            ],
        },
        {
            "id": "billing",
            "root": "apps/billing",
            "files": ["apps/billing/api.py"],
        },
    ]
    # 投影层消费入口记录，夹具按正式边界提供记录而不让投影重新读取源码。
    projected_entries: dict[str, list[dict[str, Any]]] = {}
    for node, attributes in graph.nodes(data=True):
        relative = str(attributes["source_file"])
        projected_entries.setdefault(relative, []).append(
            {
                "entryId": f"entry-{node}",
                "graphNodeId": str(node),
                "symbol": str(attributes["label"]),
                "kind": "function",
                "displayName": str(attributes["label"]),
                "entryCandidate": True,
                "contracts": list(attributes.get("contracts") or []),
                "location": {"path": relative, "startLine": 1, "endLine": 1},
            }
        )
    scope = project_scope_candidates(
        tmp_path,
        graph,
        communities={"community:shared": list(graph.nodes)},
        source_digests=source_digests,
        source_roles={path: source_role(path) for path in source_digests},
        baseline_digest="b" * 64,
        build_unit_assignments=build_unit_memberships(source_digests, build_units),
        projected_entries_by_path=projected_entries,
    )

    assert {candidate["buildUnitId"] for candidate in scope["candidates"]} == {
        "orders",
        "billing",
    }
    orders = next(
        candidate for candidate in scope["candidates"] if candidate["buildUnitId"] == "orders"
    )
    assert {entry["displayName"] for entry in orders["entries"]} == {
        "handle_order",
        "_identifier_stem",
    }
    assert orders["representativeEntries"][0]["displayName"] == "handle_order"
    # 支持文件数量保留测试源码的存在，但不提升为动作入口。
    assert orders["inventory"]["supportFileCount"] == 1


# 用集合插入节点和边，让两个 hash seed 产生不同遍历顺序。
# 在独立解释器中比较规范分区结果，验证跨进程确定性。
# 子进程执行测试自有程序文本，不接收项目提供的命令字符串。
def test_scope_partition_is_stable_across_hash_seeds_and_insertion_order() -> None:
    script = """
import json
import networkx as nx
from modus.machine.projection.community_signal import GraphEngine

graph = nx.MultiDiGraph()
for node in {"a", "b", "c", "d", "e", "f"}:
    graph.add_node(node, source_file=f"src/{node}.py")
for source, target in {
    ("a", "b"), ("b", "c"), ("c", "a"),
    ("d", "e"), ("e", "f"), ("f", "d"),
    ("c", "d"),
}:
    graph.add_edge(source, target, relation="calls", resolution="exact")
partition = GraphEngine().partition_scope(graph)
print(json.dumps(sorted(sorted(group) for group in partition.values())))
"""
    # 去重后的输出集合应只有一个值，直接暴露不同进程的分区漂移。
    outputs = {
        # NOCA:dangerous-subprocess-use-audit(当前解释器仅执行测试自有程序，变化量仅为固定 hash seed)
        subprocess.run(
            [sys.executable, "-c", script],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("1", "987654")
    }

    assert len(outputs) == 1


# 外部规则包经过真实配置入口加载，并在证据查询中保留规则 ID。
# 修改规则说明也必须改变输入基线和包身份，避免复用旧规则结果。
# 范围摘要应同步包含规则副作用计数。
def test_authoring_rule_pack_uses_the_typed_schema_and_changes_snapshot_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / ".modus").mkdir(parents=True)
    (root / "app.py").write_text("def run():\n    AUDIT = True\n", encoding="utf-8")
    (root / ".modus/config.yaml").write_text(
        """
init_analysis:
  authoring_rules:
    packs: [authoring.yaml]
    rules: []
""".lstrip(),
        encoding="utf-8",
    )
    pack = root / "authoring.yaml"
    pack.write_text(
        """
schemaVersion: 1
rules:
  - id: audit-marker
    match:
      regex: AUDIT
      suffixes: [.py]
    emit:
      type: effect
      kind: rule
      observation: audit enabled
""".lstrip(),
        encoding="utf-8",
    )

    first = analyze_repository(root, AnalysisOptions(compiler_mode="off"))
    bundle = open_analysis(root, first.identity)
    result = query_evidence(bundle, EvidenceQuery(view="evidence", paths=("app.py",)))
    assert any(item.details.get("ruleId") == "audit-marker" for item in result.items)
    scope = query_evidence(bundle, EvidenceQuery(view="scope"))
    summary = next(
        item for item in scope.items if item.details.get("recordType") == "candidateSummary"
    )
    # 从公开摘要查询核对规则信号，避免只验证内部提取器。
    capability_signals = cast(dict[str, Any], summary.details["capabilitySignals"])
    assert capability_signals["effects"]["rule"] == 1

    pack.write_text(pack.read_text(encoding="utf-8").replace("enabled", "required"))
    # 规则变化后重新分析，验证缓存身份覆盖规则包内容。
    second = analyze_repository(root, AnalysisOptions(compiler_mode="off"))
    assert second.baseline_digest != first.baseline_digest
    assert second.bundle_digest != first.bundle_digest


# 工作区只负责成员导航，初始化分析必须在项目成员内运行。
# 拒绝工作区根请求时不应创建分析缓存，保持失败路径无副作用。
def test_workspace_root_does_not_run_initialization_analysis(tmp_path: Path) -> None:
    (tmp_path / ".modus").mkdir()
    (tmp_path / ".modus/config.yaml").write_text(
        """
kind: workspace
workspace:
  members:
    - id: a
      path: members/a
      repositoryId: a
    - id: b
      path: members/b
      repositoryId: b
""".lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="workspace-root"):
        analyze_repository(tmp_path, AnalysisOptions(compiler_mode="off"))
    # 错误输入不得启动分析阶段或留下可被后续误用的状态。
    assert not (tmp_path / "modus/.cache/init-analysis").exists()
