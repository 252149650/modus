"""Optional producer failures remain scoped to their own lifecycle products.

Injected failures verify that unrelated resolution and projection results
survive, while malformed optional observations do not become trusted evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import networkx as nx

from modus.machine.capability import compose
from modus.machine.corpus import freeze
from modus.machine.facts import observe
from modus.machine.projection import lifecycle as projection_lifecycle
from modus.machine.projection.community_signal import GraphEngine
from modus.machine.resolution import lifecycle as resolution_lifecycle


def _observed(root: Path) -> tuple[object, object]:
    (root / "service.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/orders')\n"
        "def orders(): return []\n"
    )
    corpus = freeze(root, compiler_mode="off")
    activation = corpus.activate()
    activation.__enter__()
    return observe(corpus), activation


# 两个可选阶段分开注入失败，证明一个缺口不覆盖另一个阶段的结果。
# 原始异常可能包含路径或凭据，持久化摘要只应保留异常类别。
def test_resolution_degrades_reconciliation_and_topology_independently(
    tmp_path: Path, monkeypatch: Any
) -> None:
    observed, activation = _observed(tmp_path)
    try:
        original_reconcile = cast(Any, resolution_lifecycle).reconcile_graph
        calls = 0

        def flaky_reconcile(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("provider detail must not persist")
            return original_reconcile(*args, **kwargs)

        monkeypatch.setattr(resolution_lifecycle, "reconcile_graph", flaky_reconcile)
        reconciled = resolution_lifecycle.resolve(observed)  # type: ignore[arg-type]
        assert reconciled.stage_summary["resolvedGraph"]["status"] == "failed"
        assert reconciled.stage_summary["resolvedGraph"]["summary"] == "RuntimeError"

        monkeypatch.setattr(resolution_lifecycle, "reconcile_graph", original_reconcile)

        def unavailable_topology(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("absolute /private/path and token must not persist")

        monkeypatch.setattr(resolution_lifecycle, "build_topology", unavailable_topology)
        topology_fallback = resolution_lifecycle.resolve(observed)  # type: ignore[arg-type]
        assert topology_fallback.quarantine["status"] == "unavailable"
        assert topology_fallback.stage_summary["topology"]["summary"] == "RuntimeError"
        assert "/private/path" not in repr(topology_fallback.stage_summary)
    finally:
        activation.__exit__(None, None, None)  # type: ignore[attr-defined]


class _UnavailableCommunityEngine(GraphEngine):
    def partition_scope(
        self,
        graph: nx.MultiDiGraph[str],
        *,
        max_component_nodes: int = 20_000,
        max_component_edges: int = 200_000,
        max_levels: int = 16,
    ) -> dict[str, list[str]]:
        del graph, max_component_nodes, max_component_edges, max_levels
        raise RuntimeError("community engine unavailable")


# 坏行与合法行混排，验证容错不会同时丢弃可用证据。
# 社区划分失败不应阻止候选归属和未解析调用缺口的发布。
def test_projection_degrades_optional_community_and_ignores_malformed_rows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    observed, activation = _observed(tmp_path)
    try:
        resolved = resolution_lifecycle.resolve(observed)  # type: ignore[arg-type]
        capabilities = compose(resolved)
        packet_digest, packet = next(iter(resolved.source_evidence.packets.items()))
        source_path = str(packet["path"])
        resolved.source_evidence.index["files"].insert(0, "malformed")
        resolved.reference_outcomes["refs"] = [
            "malformed",
            {"callerFile": "missing.py"},
            {
                "callerFile": source_path,
                "reason": "machine.resolution.reference-unresolved",
                "name": "dynamic_target",
                "location": {"path": source_path, "startLine": 1},
            },
        ]

        def service_projection(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "nodes": [
                    "malformed",
                    {
                        "id": "orders-api",
                        "sourceAnchors": [{"path": source_path}],
                    },
                ],
                "edges": [
                    "malformed",
                    {"id": "edge", "witness": {"path": source_path}},
                ],
            }

        monkeypatch.setattr(projection_lifecycle, "project_services", service_projection)
        products = projection_lifecycle.project(
            resolved,
            capabilities,
            community_engine=_UnavailableCommunityEngine(),
        )

        assert products.stage_summary["status"] == "failed"
        assert products.stage_summary["summary"] == "RuntimeError"
        assert products.services["nodes"][1]["candidateIds"]
        assert products.services["edges"][1]["candidateIds"]
        assert any(
            gap.get("symbol") == "dynamic_target"
            for gap in products.evidence_packets[packet_digest]["coverageGaps"]
        )
    finally:
        activation.__exit__(None, None, None)  # type: ignore[attr-defined]
