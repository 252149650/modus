"""Release-archive contracts and installed-wheel acceptance scenarios.

The package is intentionally private to the repository. ``package_smoke.py``
is the stable command-line boundary; modules here separate policy, archive I/O,
and runtime acceptance checks so each can evolve without growing that entry
point into another application.
"""
