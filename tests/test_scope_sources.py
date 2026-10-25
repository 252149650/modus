"""Source reconciliation preserves identities and never grants write authority."""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from modus.cli import app
from modus.knowledge_verification.authoring_card import load_authoring_plan
from modus.paths import ModusPaths
from modus.scope_reconciliation import compare_scope_sources, validate_source_review
from modus.scope_sources import ScopeSourceError, load_material, parse_mindmap

MAP = """# Notes outside the explicit block
<!-- BEGIN -->
## 1. 内容 Content [platform=ios]
### MixedCase 阅读器
- reader-state 状态：会话状态 [platform=android]
### renderer 渲染器
<!-- END -->
## 30. 协议参考，不产卡
"""


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def manifest(root: Path, **extra: object) -> Path:
    put(root, "map.md", MAP)
    put(
        root,
        "table.csv",
        "业务,一级模块,人员,二级模块\n内容,阅读器,private-value,ignored-child\n内容,阅读器,other-private,ignored-child\n其他,阅读器,third-private,ignored-child\n",
    )
    sources = [
        {
            "id": "map",
            "format": "mindmap",
            "path": "map.md",
            "startMarker": "<!-- BEGIN -->",
            "endMarker": "<!-- END -->",
            "defaultPlatforms": ["cross-platform", "ios", "android"],
        },
        {
            "id": "table",
            "format": "csv",
            "path": "table.csv",
            "columns": {"group": "业务", "label": "一级模块"},
        },
    ]
    put(
        root,
        "input.json",
        json.dumps({"schemaVersion": 1, "sources": sources, **extra}, ensure_ascii=False),
    )
    return root / "input.json"


def test_comparison_preserves_identity_and_does_not_write(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = compare_scope_sources(str(tmp_path), str(path))
    assert result["status"] == "ok" and result["readOnly"]
    assert result["summary"]["records"] == 7
    rows = result["records"]
    mixed = next(r for r in rows if r["record_id"] == "node:MixedCase")
    assert mixed["candidateScopeRefs"] == ["module:Content/MixedCase"]
    assert mixed["platforms"] == ("cross-platform", "ios", "android")
    child = next(r for r in rows if r["record_id"] == "node:reader-state")
    assert child["status"] == "pending" and child["parent_id"] == "MixedCase"
    assert child["platforms"] == ("android",)
    table = [r for r in rows if r["sourceId"] == "table"]
    assert table[0]["candidateScopeRefs"] == ["module:Content/MixedCase"]
    assert table[1]["candidateScopeRefs"] == table[0]["candidateScopeRefs"]
    assert table[2]["status"] == "pending"
    assert all(
        row["disposition"] == "pending" and not row["scopeRefs"]
        for row in result["sourceReview"]["items"]
    )
    encoded = json.dumps(result)
    assert "private-value" not in encoded and "ignored-child" not in encoded
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert compare_scope_sources(str(tmp_path), str(path)) == result


def test_explicit_mapping_is_scoped_and_not_confirmation(tmp_path: Path) -> None:
    path = manifest(
        tmp_path,
        mappings=[
            {
                "sourceId": "table",
                "recordId": "row:4",
                "scopeRefs": ["module:Content/MixedCase", "module:Content/renderer"],
                "reason": "两个职责需拆分评审",
            }
        ],
    )
    result = compare_scope_sources(str(tmp_path), str(path))
    row = result["records"][-1]
    assert row["change"] == "split-candidate" and len(row["candidateScopeRefs"]) == 2
    assert result["sourceReview"]["items"][-1]["disposition"] == "pending"


@pytest.mark.parametrize(
    "change",
    ["duplicate-source", "unknown-record", "unknown-target", "duplicate-mapping", "bad-field"],
)
def test_malformed_input_fails_closed(tmp_path: Path, change: str) -> None:
    path = manifest(tmp_path)
    data = json.loads(path.read_text())
    mapping = {
        "sourceId": "table",
        "recordId": "row:2",
        "scopeRefs": ["module:Content/MixedCase"],
        "reason": "alias",
    }
    if change == "duplicate-source":
        data["sources"].append(data["sources"][0])
    elif change == "bad-field":
        data["confirmation"] = "confirmed"
    else:
        data["mappings"] = [mapping]
        if change == "unknown-record":
            mapping["recordId"] = "missing"
        elif change == "unknown-target":
            mapping["scopeRefs"] = ["module:Content/missing"]
        else:
            data["mappings"].append(mapping)
    path.write_text(json.dumps(data))
    with pytest.raises(ScopeSourceError):
        compare_scope_sources(str(tmp_path), str(path))


def test_same_name_and_case_collisions_are_not_merged(tmp_path: Path) -> None:
    path = manifest(
        tmp_path,
        candidates=[
            {"scopeRef": "module:Content/other", "label": "阅读器", "reason": "不同职责"},
            {"scopeRef": "module:Content/mixedcase", "label": "独立项", "reason": "碰撞"},
        ],
    )
    result = compare_scope_sources(str(tmp_path), str(path))
    assert result["identityConflicts"]
    row = next(r for r in result["records"] if r["sourceId"] == "table")
    assert row["status"] == "conflict"
    assert len(row["candidateScopeRefs"]) == 2


def test_source_locations_are_not_claimed_as_semantic_proof(tmp_path: Path) -> None:
    put(tmp_path, "source.py", "def read_content():\n    return None\n")
    path = manifest(
        tmp_path,
        candidates=[
            {
                "scopeRef": "module:Content/MixedCase",
                "label": "阅读器",
                "reason": "待核对入口",
                "locations": [
                    {"path": "source.py", "symbol": "read_content", "startLine": 1, "endLine": 2},
                    {"path": "missing.py", "symbol": "missing", "startLine": 1, "endLine": 2},
                ],
            }
        ],
    )
    result = compare_scope_sources(str(tmp_path), str(path))
    candidate = next(c for c in result["candidates"] if c["scopeRef"] == "module:Content/MixedCase")
    assert candidate["locations"][0]["semanticStatus"] == "requires-source-review"
    assert candidate["locations"][1]["status"] == "unavailable"
    assert result["status"] == "partial" and result["summary"]["unavailableLocations"] == 1


def test_unavailable_source_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    (tmp_path / "table.csv").unlink()
    result = compare_scope_sources(str(tmp_path), str(path))
    assert result["status"] == "partial" and len(result["sources"]) == 2
    assert result["sources"][1]["status"] == "unavailable"
    assert validate_source_review(result["sourceReview"], {})


def test_external_read_needs_explicit_authorization_and_rejects_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    put(tmp_path, "outside.csv", "group,label\nGroup,Item\n")
    spec = {
        "id": "external",
        "format": "csv",
        "path": str(tmp_path / "outside.csv"),
        "columns": {"group": "group", "label": "label"},
    }
    assert load_material(project, spec, ()).error
    assert load_material(project, spec, (tmp_path,)).records
    (project / "linked.csv").symlink_to(tmp_path / "outside.csv")
    assert load_material(project, {**spec, "path": "linked.csv"}, (tmp_path,)).error
    assert load_material(project, {**spec, "path": "../outside.csv"}, (tmp_path,)).error
    assert load_material(
        project, {**spec, "path": "https://example.invalid/file.csv"}, (tmp_path,)
    ).error


@pytest.mark.parametrize(
    "body",
    [
        "## 1. 域 Domain\n### item 名称\n### item 重复\n",
        "### item 无父节点\n",
        "## 无ID\n",
        "```\n## 1. 域 Domain\n",
    ],
)
def test_invalid_mindmap_has_no_partial_success(body: str) -> None:
    with pytest.raises(ScopeSourceError):
        parse_mindmap(body.encode(), {})


@pytest.mark.parametrize(
    "body", ["group,label,label\nG,a,b\n", "group,label\n,a\n", "group,label,id\nG,a,x\nG,b,x\n"]
)
def test_table_errors_are_visible(tmp_path: Path, body: str) -> None:
    put(tmp_path, "data.csv", body)
    columns = {"group": "group", "label": "label", **({"id": "id"} if ",id" in body else {})}
    result = load_material(
        tmp_path, {"id": "data", "format": "csv", "path": "data.csv", "columns": columns}, ()
    )
    assert result.error and not result.records


def test_xlsx_only_selected_columns_and_no_formula_evaluation(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Inventory"
    sheet.append(["group", "label", "private"])
    sheet.append(["Group", "Feature", "=1+1"])
    path = tmp_path / "data.xlsx"
    workbook.save(path)
    spec = {
        "id": "data",
        "format": "xlsx",
        "path": "data.xlsx",
        "sheet": "Inventory",
        "columns": {"group": "group", "label": "label"},
        "defaultPlatforms": ["desktop"],
    }
    result = load_material(tmp_path, spec, ())
    assert not result.error and result.records[0].label == "Feature"
    assert result.records[0].platforms == ("desktop",)
    assert result.records[0].platform_origin == "source-default"
    sheet["B2"] = "=1+1"
    workbook.save(path)
    error = load_material(tmp_path, spec, ()).error
    assert isinstance(error, str) and "公式" in error
    workbook.close()


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ('<!DOCTYPE x [<!ENTITY e "bad">]><x/>', "DTD"),
        ('<worksheet><row r="999999999"><c r="A999999999"/></row></worksheet>', "行数"),
        ('<worksheet><row r="1"><c r="ZZ1"/></row></worksheet>', "列数"),
        ("<worksheet><row>" + "<c/>" * 129 + "</row></worksheet>", "列数"),
    ],
)
def test_xlsx_rejects_xml_entities_and_unbounded_dimensions(
    tmp_path: Path, xml: str, expected: str
) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    original = tmp_path / "valid.xlsx"
    workbook.save(original)
    workbook.close()
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(tmp_path / "data.xlsx", "w") as archive,
    ):
        for info in source.infolist():
            archive.writestr(
                info.filename,
                xml.encode()
                if info.filename == "xl/worksheets/sheet1.xml"
                else source.read(info.filename),
            )
    result = load_material(
        tmp_path,
        {
            "id": "bad",
            "format": "xlsx",
            "path": "data.xlsx",
            "sheet": "Sheet",
            "columns": {"group": "g", "label": "l"},
        },
        (),
    )
    assert result.error and expected in result.error and not result.records


def approved_review(root: Path):
    path = manifest(root)
    review = compare_scope_sources(str(root), str(path))["sourceReview"]
    for item in review["items"]:
        item.update(disposition="excluded", reason="本轮明确排除，不删除已有知识")
    review["items"][0].update(disposition="formal", scopeRefs=["domain:Content"])
    return review


@pytest.mark.parametrize(
    "damage", ["pending", "missing", "duplicate", "foreign", "digest", "unknown-scope"]
)
def test_review_cannot_publish_unresolved_or_unaccounted_records(
    tmp_path: Path, damage: str
) -> None:
    review = approved_review(tmp_path)
    domains: dict[str, object] = {"Content": {"modules": {"MixedCase": {}}}}
    assert not validate_source_review(review, domains)
    broken = copy.deepcopy(review)
    if damage == "pending":
        broken["items"][1]["disposition"] = "pending"
    elif damage == "missing":
        broken["items"].pop()
    elif damage == "duplicate":
        broken["items"].append(broken["items"][0])
    elif damage == "foreign":
        broken["items"][0]["recordId"] = "unknown"
    elif damage == "digest":
        broken["sources"][0]["digest"] = "invalid"
    else:
        broken["items"][0]["scopeRefs"] = ["module:Content/missing"]
    assert validate_source_review(broken, domains)


def test_existing_authoring_card_stays_compatible_and_optional_review_is_checked(
    tmp_path: Path,
) -> None:
    review = approved_review(tmp_path)
    paths = ModusPaths.for_root(tmp_path)
    plan = {
        "schemaVersion": 2,
        "executionMode": "serial",
        "domains": {"Content": {"modules": {"MixedCase": {"status": "reviewed"}}}},
    }

    def write():
        put(
            tmp_path,
            paths.relative(paths.scope_confirmation_path),
            "---\n" + yaml.safe_dump({"modus": {"authoring": plan}}) + "---\n",
        )

    write()
    assert not load_authoring_plan(paths)[1]
    plan["sourceReview"] = review
    write()
    assert not load_authoring_plan(paths)[1]
    review["items"].pop()
    write()
    before = paths.scope_confirmation_path.read_bytes()
    assert any(f.code == "authoring-source-review-invalid" for f in load_authoring_plan(paths)[1])
    assert paths.scope_confirmation_path.read_bytes() == before


def test_cli_is_read_only_and_returns_machine_error_without_input_contents(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    result = CliRunner().invoke(
        app, ["knowledge", "scope-compare", "--input", str(path), "--root", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["readOnly"]
    assert not (tmp_path / "modus").exists()
    path.write_text('{"schemaVersion": 1, "secret-field": "private-value"}')
    result = CliRunner().invoke(
        app, ["knowledge", "scope-compare", "--input", str(path), "--root", str(tmp_path), "--json"]
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["status"] == "error"
    assert "private-value" not in result.output


@pytest.mark.parametrize("row_id", ["renderer", "rec:123"])
def test_table_record_id_is_not_a_module_identity(tmp_path: Path, row_id: str) -> None:
    path = manifest(tmp_path)
    data = json.loads(path.read_text())
    data["sources"][1]["columns"]["id"] = "record"
    data["sources"][1]["defaultPlatforms"] = ["desktop"]
    put(tmp_path, "table.csv", f"业务,一级模块,record\n内容,完全不同,{row_id}\n")
    path.write_text(json.dumps(data))
    result = compare_scope_sources(str(tmp_path), str(path))
    row = result["records"][-1]
    assert row["record_id"] == f"id:{row_id}"
    assert row["declared_id"] is None and row["candidateScopeRefs"] == []
    assert row["platforms"] == ("desktop",) and row["platform_origin"] == "source-default"


def test_removing_inventory_and_matching_disposition_requires_new_snapshot(tmp_path: Path) -> None:
    review = approved_review(tmp_path)
    review["sources"][-1]["recordIds"].pop()
    review["items"].pop()
    assert any(
        "摘要" in error for error in validate_source_review(review, {"Content": {"modules": {}}})
    )


def test_material_drift_changes_digest_and_bounded_reads_fail_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modus.scope_sources as sources

    path = manifest(tmp_path)
    original = compare_scope_sources(str(tmp_path), str(path))
    with (tmp_path / "table.csv").open("a") as output:
        output.write("内容,渲染器,ignored,ignored\n")
    updated = compare_scope_sources(str(tmp_path), str(path))
    assert original["inputDigest"] == updated["inputDigest"]
    assert original["sourceReview"]["inventoryDigest"] != updated["sourceReview"]["inventoryDigest"]
    assert original["sources"][1]["digest"] != updated["sources"][1]["digest"]
    monkeypatch.setattr(sources, "MAX_BYTES", 5)
    with pytest.raises(ScopeSourceError):
        compare_scope_sources(str(tmp_path), str(path))


def test_xlsx_decompression_budget_is_checked_before_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modus.scope_sources as sources

    with zipfile.ZipFile(tmp_path / "large.xlsx", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", " " * 1024)
    monkeypatch.setattr(sources, "MAX_XML_BYTES", 100)
    result = load_material(
        tmp_path,
        {
            "id": "large",
            "format": "xlsx",
            "path": "large.xlsx",
            "sheet": "Sheet",
            "columns": {"group": "g", "label": "l"},
        },
        (),
    )
    assert result.error and "超过限制" in result.error


def test_cli_reports_partial_material_without_creating_state(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    (tmp_path / "table.csv").unlink()
    result = CliRunner().invoke(
        app, ["knowledge", "scope-compare", "--input", str(path), "--root", str(tmp_path), "--json"]
    )
    assert result.exit_code == 4
    assert json.loads(result.output)["status"] == "partial"
    assert not (tmp_path / "modus").exists()


def test_source_compare_does_not_import_analysis_or_optional_spreadsheet_parser(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    path = manifest(tmp_path)
    code = "from modus.scope_reconciliation import compare_scope_sources; import sys; compare_scope_sources(sys.argv[1],sys.argv[2]); assert 'openpyxl' not in sys.modules; assert not any(n.startswith('modus.machine') for n in sys.modules)"
    subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path), str(path)], check=True)


def test_full_source_inventory_survives_authoring_card_roundtrip(tmp_path: Path) -> None:
    from modus.skillmd import split_frontmatter

    path = manifest(tmp_path)
    put(tmp_path, "table.csv", "业务,一级模块\n" + "内容,阅读器\n" * 4996)
    result = compare_scope_sources(str(tmp_path), str(path))
    assert result["summary"]["records"] == 5000
    review = result["sourceReview"]
    for item in review["items"]:
        item.update(disposition="formal", scopeRefs=["module:Content/MixedCase"], reason="r" * 500)
    domains = {"Content": {"modules": {"MixedCase": {"status": "reviewed"}}}}
    plan = {
        "schemaVersion": 2,
        "executionMode": "serial",
        "domains": domains,
        "sourceReview": review,
    }
    text = "---\n" + yaml.safe_dump({"modus": {"authoring": plan}}, allow_unicode=True) + "---\n"
    assert len(text.encode()) > 2 * 1024 * 1024
    assert not split_frontmatter(text)[0]
    paths = ModusPaths.for_root(tmp_path)
    put(tmp_path, paths.relative(paths.scope_confirmation_path), text)
    assert load_authoring_plan(paths) == (domains, [])
    review["items"].pop()
    put(
        tmp_path,
        paths.relative(paths.scope_confirmation_path),
        "---\n" + yaml.safe_dump({"modus": {"authoring": plan}}) + "---\n",
    )
    assert any(f.code == "authoring-source-review-invalid" for f in load_authoring_plan(paths)[1])


@pytest.mark.parametrize("missing_source", ["table", "map"])
def test_mapped_unavailable_source_preserves_partial_results(
    tmp_path: Path, missing_source: str
) -> None:
    mapping = {
        "sourceId": "table",
        "recordId": "row:2",
        "scopeRefs": ["module:Content/MixedCase"],
        "reason": "explicit alias",
    }
    path = manifest(tmp_path, mappings=[mapping])
    (tmp_path / ("table.csv" if missing_source == "table" else "map.md")).unlink()
    result = CliRunner().invoke(
        app, ["knowledge", "scope-compare", "--input", str(path), "--root", str(tmp_path), "--json"]
    )
    assert result.exit_code == 4, result.output
    report = json.loads(result.output)
    assert report["status"] == "partial" and report["records"]
    assert report["deferredMappings"][0]["mapping"] == mapping
    assert validate_source_review(report["sourceReview"], {})
    if missing_source == "map":
        assert report["records"][0]["status"] == "pending"
        assert report["records"][0]["candidateScopeRefs"] == []


@pytest.mark.parametrize(
    "damage", ["duplicate", "unknown-source", "empty-reason", "invalid-scope", "unknown-record"]
)
def test_partial_sources_do_not_hide_invalid_mappings(tmp_path: Path, damage: str) -> None:
    mapping = {
        "sourceId": "table",
        "recordId": "row:2",
        "scopeRefs": ["module:Content/MixedCase"],
        "reason": "explicit alias",
    }
    path = manifest(tmp_path, mappings=[mapping])
    (tmp_path / "map.md").unlink()
    data = json.loads(path.read_text())
    if damage == "duplicate":
        data["mappings"].append(mapping)
    elif damage == "unknown-source":
        data["mappings"][0]["sourceId"] = "unknown"
    elif damage == "empty-reason":
        data["mappings"][0]["reason"] = " "
    elif damage == "invalid-scope":
        data["mappings"][0]["scopeRefs"] = ["invalid"]
    else:
        data["mappings"][0]["recordId"] = "missing"
    path.write_text(json.dumps(data))
    with pytest.raises(ScopeSourceError):
        compare_scope_sources(str(tmp_path), str(path))


@pytest.mark.parametrize("target", ["input", "source", "location"])
def test_nul_paths_have_structured_cli_errors(tmp_path: Path, target: str) -> None:
    path = manifest(tmp_path)
    data = json.loads(path.read_text())
    bad_path = "bad\x00.csv"
    if target == "source":
        data["sources"][1]["path"] = bad_path
    elif target == "location":
        data["candidates"] = [
            {
                "scopeRef": "module:Content/test",
                "label": "Test",
                "reason": "check",
                "locations": [{"path": bad_path, "symbol": "x", "startLine": 1, "endLine": 1}],
            }
        ]
    path.write_text(json.dumps(data))
    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "scope-compare",
            "--input",
            bad_path if target == "input" else str(path),
            "--root",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code == (2 if target == "input" else 4), repr(result.exception)
    assert json.loads(result.output)["status"] == ("error" if target == "input" else "partial")


@pytest.mark.parametrize(
    "source_path",
    ["AGENTS.md", "./AGENTS.md", "./agents.MD", "./.CoDeBuDdY/context.md", "./Modus/knowledge.md"],
)
def test_generated_source_aliases_are_rejected(tmp_path: Path, source_path: str) -> None:
    put(tmp_path, source_path, "generated context\n")
    path = manifest(
        tmp_path,
        candidates=[
            {
                "scopeRef": "module:Content/test",
                "label": "Test",
                "reason": "check",
                "locations": [{"path": source_path, "symbol": "x", "startLine": 1, "endLine": 1}],
            }
        ],
    )
    result = compare_scope_sources(str(tmp_path), str(path))
    candidate = next(c for c in result["candidates"] if c["scopeRef"] == "module:Content/test")
    assert candidate["locations"][0]["status"] == "unavailable"
    assert result["status"] == "partial"


def test_unmatched_candidate_domain_casing_conflict_is_visible(tmp_path: Path) -> None:
    path = manifest(
        tmp_path,
        candidates=[
            {"scopeRef": "module:Sales/a", "label": "First", "reason": "check"},
            {"scopeRef": "module:sales/b", "label": "Second", "reason": "check"},
        ],
    )
    result = compare_scope_sources(str(tmp_path), str(path))
    assert ["domain:Sales", "domain:sales"] in result["identityConflicts"]
    assert result["summary"]["identityConflicts"] == 1


def test_repeated_source_locations_reuse_decoding_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modus.scope_reconciliation as reconciliation

    put(tmp_path, "source.py", "def read_content():\n    return None\n")
    path = manifest(
        tmp_path,
        candidates=[
            {
                "scopeRef": "module:Content/test",
                "label": "Test",
                "reason": "check",
                "locations": [
                    {"path": name, "symbol": "x", "startLine": 1, "endLine": 2}
                    for name in ("source.py", "./source.py", "source.py")
                ],
            }
        ],
    )
    reads = []
    decodes = []
    original = reconciliation.read_material_bytes

    class CountedBytes(bytes):
        def decode(self, *args, **kwargs):
            decodes.append(True)
            return super().decode(*args, **kwargs)

    def read(root, name, allowed_roots=()):
        reads.append(name)
        return CountedBytes(original(root, name, allowed_roots))

    monkeypatch.setattr(reconciliation, "read_material_bytes", read)
    result = compare_scope_sources(str(tmp_path), str(path))
    assert result["status"] == "ok"
    assert len(reads) == len(decodes) == 1
    locations = next(c for c in result["candidates"] if c["scopeRef"] == "module:Content/test")[
        "locations"
    ]
    assert len({loc["digest"] for loc in locations}) == 1


@pytest.mark.parametrize(
    "extra",
    [
        "extra: [" + ",".join("x" for _ in range(201)) + "]\n",
        "anchor: &a [x, y, z]\nextra: [" + ",".join("*a" for _ in range(100)) + "]\n",
        "extra: " + "[" * 65 + "x" + "]" * 65 + "\n",
        "extra: &a [*a]\n",
        "extra: !!python/object:builtins.object {}\n",
        "a0: &a0 {x: y}\n"
        + "".join(f"a{i}: &a{i} {{<<: [*a{i - 1}, *a{i - 1}]}}\n" for i in range(1, 10)),
    ],
)
def test_authoring_card_retains_yaml_safety_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    import modus.knowledge_verification.authoring_card as cards

    monkeypatch.setattr(cards, "MAX_AUTHORING_CARD_NODES", 200)
    paths = ModusPaths.for_root(tmp_path)
    plan = {"schemaVersion": 2, "executionMode": "serial", "domains": {"Content": {"modules": {}}}}
    body = yaml.safe_dump({"modus": {"authoring": plan}})
    put(tmp_path, paths.relative(paths.scope_confirmation_path), "---\n" + body + "---\n")
    assert not load_authoring_plan(paths)[1]
    put(tmp_path, paths.relative(paths.scope_confirmation_path), "---\n" + body + extra + "---\n")
    assert any(f.code == "authoring-card-invalid" for f in load_authoring_plan(paths)[1])


def test_authoring_card_retains_file_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modus.knowledge_verification.authoring_card as cards

    monkeypatch.setattr(cards, "MAX_AUTHORING_CARD_BYTES", 64)
    paths = ModusPaths.for_root(tmp_path)
    put(tmp_path, paths.relative(paths.scope_confirmation_path), "x" * 65)
    assert any(f.code == "authoring-card-unavailable" for f in load_authoring_plan(paths)[1])


def test_bounded_yaml_merges_preserve_precedence_and_defaults() -> None:
    from modus.skillmd import split_frontmatter

    text = "---\na: &a {x: first, y: default}\nb: &b {x: second, z: extra}\nmerged: {<<: [*a, *b], y: override}\n---\n"
    frontmatter, _ = split_frontmatter(text)
    assert frontmatter["merged"] == {"x": "first", "y": "override", "z": "extra"}
    assert split_frontmatter(text, max_nodes=10)[0] == {}
