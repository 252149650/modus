"""Cross-cutting persistence plane: catalog pages, bundles, integrity.

Storage is a mechanism, not a lifecycle stage: publishing commits one stage's
result atomically and verifiably, and produces no new semantic knowledge.
Stages depend on this package; it depends on no stage.
"""
