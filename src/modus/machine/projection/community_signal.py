"""Deterministic graph partitioning for precision-first Scope discovery."""

from __future__ import annotations

from typing import Any

import networkx as nx

from modus.machine.resolution.evidence_graph import (
    COMMUNITY_PROJECTION_VERSION,
    clustering_projection,
)

from ..errors import BudgetError

# Louvain determinism: the same seed and convergence threshold appear both in
# the call and in the emitted receipt so consumers can reproduce a partition.
_LOUVAIN_SEED = 42
_LOUVAIN_THRESHOLD = 1e-4
_MAX_COMPONENT_RECEIPTS = 32


class GraphEngine:
    """Partition the evidence graph without owning extraction or publication."""

    def partition_scope(
        self,
        graph: nx.MultiDiGraph[str],
        *,
        max_component_nodes: int = 20_000,
        max_component_edges: int = 200_000,
        max_levels: int = 16,
    ) -> dict[str, list[str]]:
        """Return the coarsest bounded Louvain partition with component-local IDs.

        Adding an unrelated connected component cannot alter another component's
        modularity objective, random sequence, or identifier. Each component's
        hierarchy is consumed until convergence or the explicit level budget;
        intermediate symbol-scale groups are not exposed as authoring Scope.
        """
        import hashlib
        import json
        from itertools import islice

        budgets = {
            "componentNodes": max_component_nodes,
            "componentEdges": max_component_edges,
            "levels": max_levels,
        }
        if any(type(value) is not int or value < 0 for value in budgets.values()):
            raise BudgetError("machine.projection.invalid-community-budget")
        receipt: dict[str, Any] = {
            "projectionVersion": COMMUNITY_PROJECTION_VERSION,
            "algorithm": "component-louvain-converged",
            "weight": "unit",
            "seed": _LOUVAIN_SEED,
            "threshold": _LOUVAIN_THRESHOLD,
            "budgets": budgets,
            "status": "complete-within-projection",
            "components": 0,
            "skippedComponents": 0,
            "limitedComponents": 0,
            "componentReceipts": [],
            "componentReceiptsOmitted": 0,
        }
        # Profiles may change scheduling and memory allocation, never these
        # logical limits or the selected algorithm. These are work envelopes,
        # not a hard wall-clock guarantee for each Louvain local-move pass.
        projection = clustering_projection(graph)
        result: dict[str, list[str]] = {}
        components = sorted(
            (sorted(c) for c in nx.connected_components(projection)), key=lambda c: c[0]
        )
        receipt["components"] = len(components)
        for members in components:
            component = projection.subgraph(members)
            edge_count = component.number_of_edges()
            reasons = []
            if len(members) > max_component_nodes:
                reasons.append("component-node-budget")
            if edge_count > max_component_edges:
                reasons.append("component-edge-budget")
            if max_levels == 0:
                reasons.append("level-budget")
            if reasons:
                receipt["skippedComponents"] += 1
                groups = []
                local: nx.Graph[str] = nx.Graph()
            else:
                local = nx.Graph()
                local.add_nodes_from(members)
                # A filtered NetworkX view may iterate a set of nodes.
                # Canonicalize undirected endpoints before sorting, so hash
                # seeds cannot change neighbor insertion order or Louvain ties.
                local.add_edges_from(
                    sorted(
                        (min(source, target), max(source, target))
                        for source, target in component.edges()
                    )
                )
                groups = [set(members)]
                levels = 0
                if edge_count:
                    partitions = nx.community.louvain_partitions(
                        local, weight=None, seed=_LOUVAIN_SEED, threshold=_LOUVAIN_THRESHOLD
                    )
                    for partition in islice(partitions, max_levels):
                        groups = partition
                        levels += 1
                    if levels == max_levels:
                        try:
                            next(partitions)
                        except StopIteration:
                            pass
                        else:
                            reasons.append("level-budget-convergence-unchecked")
                            receipt["limitedComponents"] += 1
            if reasons:
                receipt["status"] = "partial"
                component_id = hashlib.sha256(json.dumps(members).encode()).hexdigest()[:24]
                detail = {
                    "componentId": component_id,
                    "nodes": len(members),
                    "edges": edge_count,
                    "reasons": reasons,
                }
                if len(receipt["componentReceipts"]) < _MAX_COMPONENT_RECEIPTS:
                    receipt["componentReceipts"].append(detail)
                else:
                    receipt["componentReceiptsOmitted"] += 1
            # Louvain itself does not guarantee internally connected groups.
            for group in groups:
                for connected in nx.connected_components(local.subgraph(group)):
                    ordered = sorted(connected)
                    identity = json.dumps(
                        [projection.graph["projectionVersion"], ordered], separators=(",", ":")
                    )
                    community_id = "community:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                    result[community_id] = sorted(
                        (
                            symbol
                            for member in ordered
                            for symbol in projection.nodes[member]["members"]
                        ),
                        key=str,
                    )
        graph.graph["communityAnalysis"] = receipt
        return dict(sorted(result.items()))
