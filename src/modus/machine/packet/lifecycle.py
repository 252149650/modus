"""Package bounded projections and atomically publish their commitments.

N6 is the only lifecycle stage allowed to turn upstream products into a
consumer-facing bundle. It verifies frozen inputs immediately before publish
and never exposes an unbounded graph.
"""

from __future__ import annotations

from ..corpus.lifecycle import FrozenCorpus, SealedCorpus, portable
from ..implementation import implementation_digest
from ..progress import ProgressReporter
from ..projection.lifecycle import ProjectionProducts
from ..store.bundle import publish_bundle
from ..store.contracts import AnalysisReceipt


def package(
    corpus: FrozenCorpus,
    sealed_corpus: SealedCorpus,
    products: ProjectionProducts,
    *,
    progress: ProgressReporter | None = None,
) -> AnalysisReceipt:
    """Verify the run boundary and publish one immutable evidence bundle.

    N6 is intentionally explicit about its internal publication phases. Large
    repositories may spend meaningful time verifying, sanitizing, cataloguing,
    and authenticating evidence; progress keeps that legitimate work visible
    while the final bundle remains a single atomic state transition.
    """

    reporter = progress or ProgressReporter()
    try:
        reporter.transition("package.verify-inputs")
        corpus.snapshot.seal()
        on_verify_progress = None
        if reporter.emits_updates:

            def report_verified_inputs(completed: int, total: int) -> None:
                reporter.advance(
                    completed,
                    total,
                    unit="files",
                    force=completed == total,
                )

            on_verify_progress = report_verified_inputs
        corpus.snapshot.verify(on_progress=on_verify_progress)

        reporter.transition("package.portable.snapshot")
        snapshot = portable(corpus.paths, sealed_corpus.snapshot_payload)
        reporter.transition("package.portable.scope")
        scope = portable(corpus.paths, products.scope)
        reporter.transition("package.portable.services")
        services = portable(corpus.paths, products.services)
        reporter.transition("package.portable.diagnostics")
        diagnostics = portable(corpus.paths, products.diagnostics)
        reporter.transition("package.portable.evidence-index")
        evidence_index = portable(corpus.paths, products.evidence_index)

        reporter.transition("package.portable.evidence")
        packet_total = len(products.evidence_packets)
        evidence_packets = {}
        for completed, (digest, packet) in enumerate(products.evidence_packets.items(), start=1):
            evidence_packets[digest] = portable(corpus.paths, packet)
            if completed % 64 == 0 or completed == packet_total:
                reporter.advance(
                    completed,
                    packet_total,
                    unit="files",
                    force=completed == packet_total,
                )
        if packet_total == 0:
            reporter.advance(0, 0, unit="files", force=True)

        receipt = publish_bundle(
            corpus.paths,
            baseline_digest=sealed_corpus.baseline.digests.baseline_digest,
            implementation_digest=implementation_digest(),
            snapshot=snapshot,
            scope=scope,
            services=services,
            diagnostics=diagnostics,
            evidence_index=evidence_index,
            evidence_packets=evidence_packets,
            progress=reporter,
        )
        reporter.transition("package.complete")
        return receipt
    finally:
        reporter.finish()


__all__ = ["package"]
