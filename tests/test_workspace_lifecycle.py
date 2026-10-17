"""Workspace lifecycle never mutates member repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from modus import __version__
from modus.commands import uninstall
from modus.commands.init_project import run_workspace_init
from modus.config import ModusConfig
from modus.manifest import Manifest
from modus.paths import ModusPaths


# 工作区只引用成员仓库，成员是否安装 Modus 不影响此所有权规则。
# 测试覆盖全新、混合、全部已安装三种状态。
# 使用真实 Git 根模拟成员发现，防止普通目录偶然满足前置条件。
# 成员全部文件的字节快照同时保护源码与已有清单。
# 外层权威技能与宿主投影均须生成，避免“零写入”假实现通过。
# readOnly 和 referenced 是报告契约，调用者无需再次推断成员处理方式。
@pytest.mark.parametrize(
    "member_states",
    [
        ("missing", "missing"),
        ("installed", "missing"),
        ("installed", "installed"),
    ],
)
def test_workspace_init_only_writes_outer_root_across_member_install_states(
    tmp_path: Path, member_states: tuple[str, str]
) -> None:
    members = [tmp_path / "teams/service-a", tmp_path / "clients/service-b"]
    for member, state in zip(members, member_states, strict=True):
        member.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(member)], check=True)
        (member / "source.txt").write_text(f"{state}\n", encoding="utf-8")
        if state == "installed":
            Manifest(
                framework_version=__version__,
                platforms=["codex"],
            ).write(ModusPaths.for_root(member))

    before = {
        member: {
            path.relative_to(member).as_posix(): path.read_bytes()
            for path in member.rglob("*")
            if path.is_file()
        }
        for member in members
    }

    result = run_workspace_init(
        tmp_path,
        ["codex"],
        ["teams/service-a", "clients/service-b"],
    )

    after = {
        member: {
            path.relative_to(member).as_posix(): path.read_bytes()
            for path in member.rglob("*")
            if path.is_file()
        }
        for member in members
    }
    assert after == before
    assert result["kind"] == "workspace"
    assert all(item["readOnly"] for item in result["memberResults"])
    assert {item["status"] for item in result["memberResults"]} == {"referenced"}
    assert ModusConfig.read(ModusPaths.for_root(tmp_path)).kind == "workspace"
    assert (tmp_path / ".modus/skills/modus-init/references/workspace-init.md").is_file()
    assert (tmp_path / ".agents/skills/modus-init/references/workspace-init.md").is_file()


# 明确请求删除外层知识时，成员仓库仍不属于删除范围。
# 比较成员完整快照，而非仅检查一个代表文件。
# 外层知识确已删除，证明测试执行了实际卸载流程。
# 外层与成员生命周期独立，不能依赖成员安装状态决定是否清理。
def test_workspace_uninstall_never_traverses_member_projects(tmp_path: Path) -> None:
    members = [tmp_path / "teams/service-a", tmp_path / "clients/service-b"]
    for member in members:
        member.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(member)], check=True)
        (member / "source.txt").write_text("member-owned\n", encoding="utf-8")
    run_workspace_init(
        tmp_path,
        ["codex"],
        ["teams/service-a", "clients/service-b"],
    )
    before = {
        member: {
            path.relative_to(member).as_posix(): path.read_bytes()
            for path in member.rglob("*")
            if path.is_file()
        }
        for member in members
    }

    result = uninstall.run_uninstall(tmp_path, keep_knowledge=False, apply=True)

    assert result["projectKind"] == "workspace"
    assert result["knowledge"]["disposition"] == "delete"
    assert not (tmp_path / "modus/knowledge").exists()
    after = {
        member: {
            path.relative_to(member).as_posix(): path.read_bytes()
            for path in member.rglob("*")
            if path.is_file()
        }
        for member in members
    }
    assert after == before


# 成员在初始化后移动，模拟导航信息随仓库布局过时。
# 刷新时应读取仍存在的成员，并显式报告失联成员。
# 保留可用映射使部分失败仍有可读结果，不伪造完整成功。
# 工作区导航不需要建立成员分析缓存，避免越过只读约束。
def test_workspace_maps_use_live_members_and_report_partial_failure(tmp_path: Path) -> None:
    for name in ["a", "b"]:
        member = tmp_path / "teams" / name
        member.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(member)], check=True)
        (member / "main.ts").write_text("export function run() {}")
    result = run_workspace_init(tmp_path, ["codex"], ["teams/a", "teams/b"])
    assert all(item["readOnly"] for item in result["memberResults"])
    paths = ModusPaths.for_root(tmp_path)
    assert paths.config_path == tmp_path / ".modus/config.yaml"
    assert paths.config_path.is_file()
    data_entries = {entry.name for entry in paths.data_dir.iterdir()}
    assert "knowledge" in data_entries
    assert data_entries <= {"knowledge", ".cache"}
    from modus.workspace import refresh_generated

    (tmp_path / "teams/b").rename(tmp_path / "teams/moved")
    outcome = refresh_generated(tmp_path)
    assert outcome["status"] == "partial"
    assert outcome["unavailableMembers"]
    assert not (tmp_path / "teams/a/modus").exists()
    assert "main.ts" in paths.workspace_generated_map("system-map").read_text(encoding="utf-8")
    assert not paths.init_analysis_dir.exists()
