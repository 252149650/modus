"""Stable command-line entry point for the Modus release smoke checks."""

from release_smoke.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
