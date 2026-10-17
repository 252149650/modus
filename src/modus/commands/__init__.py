"""Application-level command orchestration for Modus.

Each command module coordinates one CLI use case and returns a structured,
JSON-ready result; the :mod:`modus.cli_commands` adapters own argument parsing,
terminal I/O, and exit-code translation. Presentation follows the same split:
``*_report`` modules render one command's results for humans, and
:mod:`modus.commands.reporting` holds the visual vocabulary they share.
Cross-cutting safety infrastructure lives beside the commands that need it:
:mod:`modus.commands.install_state` (protected-install checks and exact
rollback) and :mod:`modus.commands.permission_errors` (permission-failure
classification).

This package deliberately performs no eager imports and defines no convenience
re-exports. Callers import the command module they depend on explicitly, keeping
the dependency graph visible and preventing unrelated commands from acquiring
import-time coupling.
"""
