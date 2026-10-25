"""Domain topics retain ordinary Markdown identity, retrieval and write safety."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from modus.commands.verify_knowledge import run_verify_knowledge
from modus.knowledge_identity import KnowledgePathIdentity
from modus.knowledge_update import update_knowledge
from modus.knowledge_verification.discovery import discover_knowledge
from modus.orchestrator import knowledge_sync
from modus.paths import DOMAIN_GENERATED_REFERENCE_KINDS, LEGACY_DOMAIN_REFERENCE_KINDS, ModusPaths
from modus.retrieval import runtime
from modus.retrieval.index import scan_index
from modus.retrieval.stages.documents import classify_document, is_rule_evidence
from modus.retrieval.stages.selection import select_query_targets
from modus.workspace import _canonical_knowledge_observation

PREFIX = "modus/knowledge/domain/tasks/references/"
ROUTER = "modus/knowledge/domain/tasks/SKILL.md"
MODULE = PREFIX + "modules/export.md"
TOPICS = ("api", "downstream", "configuration", "lifecycle")
LEGACY = ("api", "downstream", "object", "rule", "state_machine", "infrastructure")


def _put(root: Path, relative: str, text: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _project(root: Path) -> ModusPaths:
    _put(root, MODULE, "# Export\n\nExport a document.\n")
    _put(root, ROUTER, "# Tasks\n\n[Export](references/modules/export.md)\n")
    _put(
        root,
        "modus/knowledge/architecture-overview.md",
        "# System\n\n[Tasks](domain/tasks/SKILL.md)\n",
    )
    return ModusPaths.for_root(root)


@pytest.mark.parametrize(
    "name",
    [*TOPICS, "object", "rule", "state_machine", "infrastructure", "api/legacy", "object/legacy"],
)
def test_topics_keep_domain_identity_and_legacy_compatibility(tmp_path: Path, name: str) -> None:
    paths = _project(tmp_path)
    relative = PREFIX + name + ".md"
    _put(tmp_path, relative, "# Contract\n\nExisting behavior remains supported.\n")
    identity = KnowledgePathIdentity.classify(paths, relative)
    assert identity is not None
    assert identity.path_kind == "domain-generated-reference"
    assert identity.domain == "tasks" and identity.module is None
    assert identity.ownership_plane == "machine"
    assert classify_document(relative)[2]["module"] is None
    inventory = discover_knowledge(paths, paths.knowledge_dir)
    assert [paths.relative(item.path) for item in inventory.modules] == [MODULE]
    assert run_verify_knowledge(str(tmp_path))["status"] == "clean"


@pytest.mark.parametrize("name", TOPICS)
def test_topic_cas_sync_and_retirement(tmp_path: Path, name: str) -> None:
    _project(tmp_path)
    target = PREFIX + name + ".md"
    original, changed = "# Original\n", "# Changed\n"
    assert update_knowledge(str(tmp_path), {target: original})["writes"] == [target]
    digest = hashlib.sha256(original.encode()).hexdigest()
    assert update_knowledge(str(tmp_path), {target: changed}, expected_hashes={target: digest})[
        "writes"
    ] == [target]
    conflict = update_knowledge(
        str(tmp_path), {}, deletions=[target], expected_hashes={target: digest}
    )
    assert conflict["conflicts"]
    assert (tmp_path / target).read_text() == changed
    sync = knowledge_sync.sync_knowledge(str(tmp_path), targets=[target])
    assert sync["candidates"][0]["moduleId"] is None
    current = hashlib.sha256(changed.encode()).hexdigest()
    assert update_knowledge(
        str(tmp_path), {}, deletions=[target], expected_hashes={target: current}
    )["deleted"] == [target]


def test_topic_symlink_does_not_acquire_ownership(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    outside = _put(tmp_path, "outside.md", "untouched\n")
    target = PREFIX + "configuration.md"
    (tmp_path / target).symlink_to(outside)
    assert KnowledgePathIdentity.classify(paths, target) is None
    assert update_knowledge(str(tmp_path), {target: "# Replacement\n"})["status"] == "partial"
    assert outside.read_text() == "untouched\n"


def test_unsupported_nested_paths_are_not_new_modules(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    for relative in (PREFIX + "modules/export/settings.md", PREFIX + "shared/settings.md"):
        _put(tmp_path, relative, "# Settings\n")
        assert KnowledgePathIdentity.classify(paths, relative) is None
        assert classify_document(relative)[2]["module"] is None
    assert "module-path-invalid" in run_verify_knowledge(str(tmp_path))["counts"]


@pytest.mark.parametrize("name", ["configuration", "rule"])
def test_related_rules_remain_retrievable_without_loading_unrelated_topics(
    tmp_path: Path, name: str
) -> None:
    paths = _project(tmp_path)
    target = PREFIX + name + ".md"
    key = "export_gate_fixture"
    _put(tmp_path, target, f"# Rules\n## Export\n### Gate\n{key} requires an explicit opt-in.\n")
    unrelated = PREFIX + "lifecycle.md"
    _put(tmp_path, unrelated, "# Connections\n\nUnrelated resources.\n")
    inventory = discover_knowledge(paths, paths.knowledge_dir)
    selected, _partial, _receipt = select_query_targets(paths, list(inventory.documents), key)
    relatives = {paths.relative(path) for path in selected}
    assert {target, ROUTER} <= relatives
    assert MODULE not in relatives and unrelated not in relatives
    records = [
        row for row in scan_index(paths, kind="project")["sections"] if row["path"] == target
    ]
    assert any(is_rule_evidence(row) for row in records)


@pytest.mark.parametrize("name", ["configuration", "rule"])
def test_change_context_keeps_rule_preconditions_and_recovery(tmp_path: Path, name: str) -> None:
    from modus.orchestrator.context_resolver import resolve_context

    _project(tmp_path)
    target = PREFIX + name + ".md"
    _put(
        tmp_path,
        target,
        "# Rules\n## Export\n### Write\nOnly the active session writes.\n"
        "#### Failure\nexport_failure_fixture keeps the previous file.\n"
        "#### Recovery\nRetry with the same identity.\n",
    )
    result = resolve_context(
        str(tmp_path),
        "export_failure_fixture",
        scope="project-read",
        explicit_paths=[target],
        purpose="change",
    )
    excerpts = [row["excerpt"] for row in result["evidence"] if row.get("path") == target]
    assert excerpts, result
    assert any("active session" in text and "Recovery" in text for text in excerpts)


def test_configuration_does_not_promote_pending_rules(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    target = PREFIX + "configuration.md"
    _put(
        tmp_path,
        target,
        "# Configuration\n## 待确认项\n### Export retry\nRetry policy is undecided.\n",
    )
    row = next(
        row
        for row in scan_index(paths, kind="project")["sections"]
        if row["heading"] == "Export retry"
    )
    assert row["facet"] == "rule" and row["status"] == "hypothesis"
    assert not is_rule_evidence(row)


def test_lifecycle_entry_keeps_conditions_and_recovery(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    target = PREFIX + "lifecycle.md"
    _put(
        tmp_path,
        target,
        "# Lifecycle\n## Export\n### Write\nOnly the active session writes.\n#### Failure\nKeep the previous file.\n#### Recovery\nRetry with the same identity.\n### Another task\nUnrelated operation.\n",
    )
    index = scan_index(paths, kind="project")
    failure = next(
        row for row in index["sections"] if row["path"] == target and row["heading"] == "Failure"
    )
    assert failure["facet"] == "state_machine"
    excerpt = runtime._entry_refs(index, [failure])[0]["excerpt"]
    assert "active session" in excerpt and "Recovery" in excerpt
    assert "Unrelated operation" not in excerpt


@pytest.mark.parametrize(
    "topics",
    [TOPICS, LEGACY, (*LEGACY, "configuration"), (*LEGACY, "lifecycle"), (*TOPICS, "object")],
)
def test_workspace_recognizes_new_and_legacy_complete_domains(
    tmp_path: Path, topics: tuple[str, ...]
) -> None:
    paths = _project(tmp_path)
    for name in topics:
        _put(tmp_path, PREFIX + name + ".md", f"# {name}\n\nDocumented behavior.\n")
    state, _evidence, missing, issues = _canonical_knowledge_observation({"path": "."}, paths)
    assert state == "available" and not missing and not issues


@pytest.mark.parametrize(
    ("topics", "missing_name"),
    [(TOPICS[:-1], "lifecycle.md"), (LEGACY[:-1], "infrastructure.md"), ((), "configuration.md")],
)
def test_workspace_missing_paths_follow_the_selected_layout(
    tmp_path: Path, topics: tuple[str, ...], missing_name: str
) -> None:
    paths = _project(tmp_path)
    for name in topics:
        _put(tmp_path, PREFIX + name + ".md", f"# {name}\n")
    state, _evidence, missing, _issues = _canonical_knowledge_observation({"path": "."}, paths)
    assert state == "partial"
    assert any(path.endswith(missing_name) for path in missing)


def test_topic_contracts_are_projected_without_new_metadata(tmp_path: Path) -> None:
    from modus.scaffold import TemplateCatalog, scaffold_project

    assert DOMAIN_GENERATED_REFERENCE_KINDS == TOPICS
    assert set(LEGACY_DOMAIN_REFERENCE_KINDS) == set(LEGACY)
    scaffold_project(tmp_path, ["codebuddy"])
    catalog = TemplateCatalog()
    for name in ("topics", "api", "collaboration", "configuration", "lifecycle", "module"):
        relative = f"references/authoring/framework/deliverables/{name}.md"
        expected = catalog.read(f"skills/using-modus/{relative}")
        for base in (".modus/skills", ".codebuddy/skills"):
            assert (tmp_path / base / "using-modus" / relative).read_text() == expected
