"""Application services that coordinate Modus's evidence layers.

The package sits between command adapters and domain infrastructure:

* ``preflight`` observes installation and enhancement health;
* ``context_resolver`` composes bounded narrative and current-source evidence;
* ``knowledge_sync`` identifies documents affected by implementation changes;
* ``fallback`` preserves the established search API over focused search modules.

Modules are intentionally not imported here. Keeping the package boundary lazy
lets lightweight CLI commands start without loading retrieval internals.
"""

__all__ = ["context_resolver", "fallback", "knowledge_sync", "module_meta", "preflight"]
