"""Stable repository coordinates for tests that inspect source assets."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

__all__ = ["REPO_ROOT"]
