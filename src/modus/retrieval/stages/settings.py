"""Backward-compatible settings imports for the historical stages namespace."""

from ..settings import RetrievalSettings, retrieval_settings

_settings = retrieval_settings

__all__ = ["RetrievalSettings", "retrieval_settings"]
