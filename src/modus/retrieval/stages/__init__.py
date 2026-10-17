"""Composable stages for a live Markdown index; no persistent service required.

Each stage has one boundary: ``discovery`` owns filesystem safety, ``selection``
bounds I/O, ``documents`` preserves semantic entry ranges, ``lexical`` builds
BM25 statistics, and ``ranking`` fuses incomparable signals deterministically.
The ``sections`` and ``settings`` modules are compatibility facades.
"""
