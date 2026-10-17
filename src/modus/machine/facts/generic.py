"""Bounded text evidence for source languages without a dedicated parser."""

from __future__ import annotations

from typing import Any

from modus.machine.corpus.source_snapshot import SourcePath
from modus.machine.facts.language_capabilities import capability_for
from modus.machine.store.ids import make_id


# contains 的精确性仅说明文本块属于文件，不代表已解析其中的调用关系。
def extract_generic_text(path: SourcePath) -> dict[str, Any]:
    """Emit bounded text-chunk evidence for a file with no dedicated parser.

    Produces one file node plus one ``source-text`` node per 120-line chunk,
    all marked ``analysis_completeness: fallback`` so consumers can tell text
    evidence apart from parsed symbols.
    """
    text = path.read_text(encoding="utf-8")
    capability = capability_for(str(path))
    root_id = make_id(str(path))
    lines = text.splitlines()
    nodes = [
        {
            "id": root_id,
            "label": path.name,
            "kind": "file",
            "file_type": "code",
            "source_file": str(path),
            "source_location": "L1",
            "source_end_line": max(1, len(lines)),
            "language": capability.language,
            "analysis_completeness": "fallback",
        }
    ]
    edges = []
    for offset in range(0, len(lines), 120):
        chunk = lines[offset : offset + 120]
        if not any(line.strip() for line in chunk):
            continue
        node_id = make_id(path.parent.name, path.stem, "text", str(offset + 1))
        nodes.append(
            {
                "id": node_id,
                "label": f"{path.name}:L{offset + 1}",
                "kind": "source-text",
                "file_type": "document" if capability.role == "document" else "code",
                "source_file": str(path),
                "source_location": f"L{offset + 1}",
                "source_end_line": offset + len(chunk),
                "signature": chunk[0][:400],
                "language": capability.language,
                "analysis_completeness": "fallback",
                "resolution": "unresolved",
            }
        )
        edges.append(
            {
                "source": root_id,
                "target": node_id,
                "relation": "contains",
                "source_file": str(path),
                "source_location": f"L{offset + 1}",
                "resolution": "exact",
                "binding_kind": "declared-exact",
            }
        )
    return {
        "nodes": nodes,
        "edges": edges,
        "analysisCompleteness": "fallback",
        "diagnostics": [
            {
                "code": "machine.facts.generic-text",
                "path": str(path),
                "message": "Text evidence available; symbol/call resolution unavailable.",
            }
        ],
    }
