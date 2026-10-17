"""Live, bounded retrieval over project knowledge and current source.

The package follows a staged architecture:

``query -> discovery -> selection -> documents -> ranking -> pipeline -> delivery``

``runtime`` is the stable integration facade, while ``index`` exposes the
ephemeral Markdown index for callers that need lower-level search control.
"""
