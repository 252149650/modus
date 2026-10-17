"""Portability preserves mappings even when path normalization creates collisions.

The fixture deliberately includes keys with overlapping portable forms; losing
either value would make the sealed input description incomplete.
"""

from __future__ import annotations

from pathlib import Path

from modus.machine.corpus.lifecycle import portable
from modus.paths import ModusPaths


def test_portable_normalizes_mapping_keys_and_preserves_collisions(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    paths = ModusPaths.for_root(root)
    first = Path.home() / ".cache/one/settings.xml"
    second = Path.home() / ".cache/two/settings.xml"

    result = portable(
        paths,
        {
            "files": {
                f"external:{first}": {"path": str(first), "digest": "b" * 64},
                f"external:{second}": {"path": str(second), "digest": "a" * 64},
                str(root / "pom.xml"): {"path": str(root / "pom.xml")},
            }
        },
    )

    files = result["files"]
    assert set(files) == {
        "./pom.xml",
        "external:settings.xml#1",
        "external:settings.xml#2",
    }
    assert files["external:settings.xml#1"]["digest"] == "a" * 64
    assert str(root) not in str(result)
    assert str(Path.home()) not in str(result)
