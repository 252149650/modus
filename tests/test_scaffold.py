"""Scaffold catalog integrity and all-or-nothing installation."""

from __future__ import annotations

from pathlib import Path

import pytest

from _support.markdown import assert_local_markdown_links_resolve
from modus.paths import ModusPaths
from modus.scaffold import (
    ProjectScaffolder,
    ScaffoldRequest,
    ScaffoldStateError,
    TemplateCatalog,
    TemplateFormatError,
    TemplateNotFoundError,
    scaffold_project,
    templates_dir,
)
from modus.scaffold.writer import ScaffoldAsset
from modus.surface import CORE_SKILLS, EXPLICIT_COMMANDS


# 模板随安装包离线使用，引用必须在自身目录内闭合。
# 按公共目录顺序加载，避免发布时缺失技能却仍能导入 Python 包。
def test_packaged_template_tree_is_complete_and_self_contained() -> None:
    catalog = TemplateCatalog()
    assert [skill.name for skill in catalog.load_skills(CORE_SKILLS)] == list(CORE_SKILLS)
    assert [command.name for command in catalog.load_commands(EXPLICIT_COMMANDS)] == list(
        EXPLICIT_COMMANDS
    )

    for path in templates_dir().rglob("*.md"):
        template_id = path.relative_to(templates_dir()).as_posix()
        assert catalog.read(template_id) == path.read_text(encoding="utf-8")
        assert_local_markdown_links_resolve(path, root=templates_dir())


# 空描述会产生无意义的命令入口，必须在渲染前拒绝。
def test_catalog_rejects_invalid_command_metadata(tmp_path: Path) -> None:
    command = tmp_path / "commands/example.md"
    command.parent.mkdir(parents=True)
    command.write_text("---\ndescription: ''\n---\n\n# /modus-example\n", encoding="utf-8")
    catalog = TemplateCatalog(tmp_path)

    with pytest.raises(TemplateFormatError, match="非空 description"):
        catalog.load_commands(("example",))


# 重复名称是调用者契约错误，不能静默覆盖同名命令。
def test_catalog_rejects_duplicate_command_names(tmp_path: Path) -> None:
    catalog = TemplateCatalog(tmp_path)

    with pytest.raises(TemplateFormatError, match="duplicate command"):
        catalog.load_commands(("example", "example"))


# 统一末尾换行使模板拼接和差异审查保持确定性。
def test_catalog_rejects_a_template_without_a_trailing_newline(tmp_path: Path) -> None:
    template = tmp_path / "plain.md"
    template.write_text("# no newline", encoding="utf-8")

    with pytest.raises(TemplateFormatError, match="以换行结束"):
        TemplateCatalog(tmp_path).read("plain.md")


# 故意使用缺失目录验证预检顺序：资源不完整时不能留下半次安装。
# 目录为空的断言覆盖副作用，而不只是异常类型。
def test_project_scaffolder_validates_catalog_before_first_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    request = ScaffoldRequest(platforms=("codex",))
    scaffolder = ProjectScaffolder(
        paths=ModusPaths.for_root(project),
        request=request,
        catalog=TemplateCatalog(tmp_path / "missing-templates"),
    )

    with pytest.raises(TemplateNotFoundError, match="commands/init.md"):
        scaffolder.render()

    assert list(project.iterdir()) == []


# 损坏清单意味着无法证明资产所有权，应停止而非覆盖恢复。
# 同时检查损坏文件和无关哨兵，确保失败没有写入副作用。
def test_corrupt_existing_manifest_fails_closed_without_writes(tmp_path: Path) -> None:
    manifest = tmp_path / ".modus/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{broken", encoding="utf-8")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")

    with pytest.raises(ScaffoldStateError, match="无法安全合并"):
        scaffold_project(tmp_path, ["codex"])

    assert manifest.read_text(encoding="utf-8") == "{broken"
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"


# 资产路径在构造时就受约束，渲染器无需猜测上级路径的意图。
def test_scaffold_asset_rejects_paths_outside_project() -> None:
    with pytest.raises(ValueError, match="规范的项目相对"):
        ScaffoldAsset(path="../escape.md", content="no\n", template="commands/no.md")


# 工作区手册复用随包权威模板，避免维护两份逐渐分叉的规则。
def test_workspace_router_uses_the_packaged_authoring_contract() -> None:
    from modus.workspace import handbook_skill_text

    assert handbook_skill_text() == TemplateCatalog().read("knowledge/handbook/SKILL.md")
