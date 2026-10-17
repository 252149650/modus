"""Workspace application services with strict member-isolation boundaries.

A workspace aggregates navigation across independently owned Git roots; it
never turns discovery into authority and never initializes a member as a side
effect.  This module separates four concerns: confirmed member validation,
read-only knowledge inventory, rebuildable map projection, and bounded routing
to member-local services.  Every member reports an independent outcome so one
unavailable repository cannot erase useful evidence from the others.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from .artifacts_io import CorruptArtifactError
from .config import ModusConfig
from .paths import (
    DOMAIN_GENERATED_REFERENCE_KINDS,
    PROJECT_OUTPUT_DIRS,
    ModusPaths,
)
from .workspace_maps import MAP_NAMES
from .workspace_maps import member_group as _member_group
from .workspace_maps import member_markdown as _member_markdown
from .workspace_maps import render_maps as _render_maps

if TYPE_CHECKING:
    from .retrieval.budget import RetrievalBudget

JsonObject: TypeAlias = dict[str, Any]
WorkspaceMember: TypeAlias = Mapping[str, Any]

_IGNORED_DISCOVERY_DIRS: Final = {
    ".git",
    ".hg",
    ".svn",
    ".modus",
    "modus",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "coverage",
    *PROJECT_OUTPUT_DIRS,
}


_MEMBER_ID_RE: Final = re.compile(r"[^a-zA-Z0-9._-]+")


# 固定键序和分隔符，使相同元数据具有可比较的表示。
def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 发现只产生候选路径，成员授权仍由确认流程完成。
# 遇到 Git 根即停止向下遍历，避免把成员内部仓库重复纳入。
def discover_members(root: str | Path, *, max_depth: int = 4) -> list[str]:
    """Discover nested Git/worktree roots, without crossing a discovered root."""
    base = Path(root).resolve()
    if max_depth < 1:
        return []
    found: list[str] = []
    for current, dirnames, _filenames in os.walk(base, followlinks=False):
        here = Path(current)
        try:
            relative = here.relative_to(base)
        except ValueError:
            continue
        depth = len(relative.parts)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _IGNORED_DISCOVERY_DIRS
            and not (here / name).is_symlink()
            and not (name.startswith(".") and name != ".git")
        )
        if depth > 0 and ((here / ".git").is_dir() or (here / ".git").is_file()):
            found.append(relative.as_posix())
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
    return sorted(found)


# 显示名称由末级目录派生；碰撞由调用方统一消解。
def _member_id(relative_path: str) -> str:
    candidate = _MEMBER_ID_RE.sub("-", Path(relative_path).name).strip("-.")
    return candidate or "member"


# 在解析真实路径前检查每一级链接，保留路径边界的判断依据。
# 同时接受普通 Git 仓库和使用 .git 文件的工作树。
def _validated_member_root(base: Path, raw: str) -> tuple[str, Path]:
    """Bind one canonical, link-free relative Git root below ``base``."""

    relative = Path(raw)
    if (
        not relative.parts
        or relative.is_absolute()
        or "\\" in raw
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"工作区成员必须为规范相对路径： {raw!r}")
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"工作区成员路径包含符号链接： {raw!r}")
    target = (base / relative).resolve()
    try:
        canonical = target.relative_to(base).as_posix()
    except ValueError as exc:
        raise ValueError(f"工作区成员越出工作区根目录： {raw!r}") from exc
    if target.is_symlink() or not target.is_dir():
        raise ValueError(f"工作区成员不是常规目录： {canonical}")
    if not ((target / ".git").is_dir() or (target / ".git").is_file()):
        raise ValueError(f"工作区成员不是 Git 仓库或工作树根目录： {canonical}")
    return canonical, target


# 编号只依赖已排序的成员和已用名称，保证重复初始化结果稳定。
def _unused_member_id(relative_path: str, used_ids: set[str]) -> str:
    """Derive a stable member ID, adding the smallest collision suffix."""

    base_id = _member_id(relative_path)
    candidate = base_id
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base_id}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


# 未安装成员允许使用工作区身份；已安装成员必须保持项目类型。
# 读取失败不应把只读成员登记变成强制安装。
def _member_repository_id(target: Path, member_id: str, canonical: str) -> str:
    """Use a member's explicit topology identity when valid and available."""

    try:
        child_config = ModusConfig.read_available(ModusPaths.for_root(target))
    except (FileNotFoundError, OSError, ValueError):
        return member_id
    if child_config.kind != "project":
        raise ValueError(f"工作区成员 {canonical} 必须为 kind: project，实际为 {child_config.kind}")
    return child_config.init_analysis.topology.repository_id or member_id


# 一次验证整个确认集合，再交付规范成员表。
# 先排序后分配 ID，消除调用方输入顺序带来的身份漂移。
def normalize_members(root: str | Path, relative_paths: Iterable[str]) -> list[JsonObject]:
    """Validate user-confirmed roots and derive stable member metadata."""
    base = Path(root).resolve()
    members: list[JsonObject] = []
    used_ids: set[str] = set()
    for raw in sorted(relative_paths, key=str):
        canonical, target = _validated_member_root(base, str(raw))
        member_id = _unused_member_id(canonical, used_ids)
        repository_id = _member_repository_id(target, member_id, canonical)
        members.append(
            {
                "id": member_id,
                "path": canonical,
                "repositoryId": repository_id,
            }
        )
    # 单仓库使用项目模式，工作区成员集合至少包含两个独立根。
    if len(members) < 2:
        raise ValueError("工作区初始化需要至少两个已确认成员")
    paths = [member["path"] for member in members]
    # 路径唯一性独立于显示 ID，重复确认同一仓库不能形成两个成员。
    if len(paths) != len(set(paths)):
        raise ValueError("工作区成员路径必须唯一")
    return sorted(members, key=lambda item: item["path"])


# 使用 lstat 检查路径本身，避免跟随链接后丢失越界证据。
# 缺失路径由上层解释为缺少产物，不在此处创建目录。
def _unsafe_path_component(authority_root: Path, target: Path) -> str | None:
    try:
        relative = target.relative_to(authority_root)
    except ValueError:
        return f"路径越出成员根目录： {target}"
    current = authority_root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"路径组成部分不可读： {current}: {exc}"
        if stat.S_ISLNK(info.st_mode):
            return f"符号链接不安全： {current}"
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return f"父路径不是目录： {current}"
    return None


# 通过修改遍历目录集合隔离链接，同时保留可读的诊断。
def _safe_discovery_directories(here: Path, dirnames: list[str], issues: list[str]) -> list[str]:
    """Partition an ``os.walk`` directory batch without following links."""

    safe: list[str] = []
    for name in sorted(dirnames):
        candidate = here / name
        if candidate.is_symlink():
            issues.append(f"符号链接不安全： {candidate}")
        else:
            safe.append(name)
    return safe


# 空文档不能证明知识可用；读取错误则作为证据缺口保留。
def _nonempty_markdown(candidate: Path, issues: list[str]) -> bool:
    """Return whether one path is readable non-empty Markdown, recording gaps."""

    if candidate.suffix.lower() != ".md":
        return False
    if candidate.is_symlink() or not candidate.is_file():
        issues.append(f"知识文档不安全： {candidate}")
        return False
    try:
        return bool(candidate.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(f"知识文档不可读： {candidate}: {exc}")
        return False


# 知识扫描独立返回发现结果和问题，允许部分成员继续提供证据。
# 该过程只读，不修复成员目录或安装状态。
def _scan_markdown_payloads(authority_root: Path, root: Path) -> tuple[list[Path], list[str]]:
    """Read-only, no-follow inventory for non-empty Markdown below one knowledge root."""
    unsafe = _unsafe_path_component(authority_root, root)
    if unsafe:
        return [], [unsafe]
    if not root.is_dir():
        return [], []
    found: list[Path] = []
    issues: list[str] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        dirnames[:] = _safe_discovery_directories(here, dirnames, issues)
        for name in sorted(filenames):
            candidate = here / name
            if _nonempty_markdown(candidate, issues):
                found.append(candidate)
    return found, issues


# 成员内证据加上成员前缀后，才进入工作区统一命名空间。
def _workspace_evidence_path(member: Mapping[str, Any], paths: ModusPaths, target: Path) -> str:
    return (Path(str(member["path"])) / paths.relative(target)).as_posix()


# 清单存在与清单有效是两个状态，损坏清单仍报告已发现安装。
# 调用方可分别展示安装问题与知识可用性。
def _installation_observation(paths: ModusPaths) -> tuple[bool, str | None]:
    target = paths.manifest_path
    unsafe = _unsafe_path_component(paths.root, target)
    if unsafe:
        return False, unsafe
    if not target.exists():
        return False, None
    if not target.is_file():
        return False, ".modus/manifest.json 不是常规文件"
    try:
        from .manifest import Manifest

        Manifest.read(paths)
    except (CorruptArtifactError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return True, str(exc)
    return True, None


# 缺失文件与不可读文件分别返回，避免把权限故障误报为空知识。
def _read_nonempty_markdown(authority_root: Path, target: Path) -> tuple[bool, str | None]:
    unsafe = _unsafe_path_component(authority_root, target)
    if unsafe:
        return False, unsafe
    if not target.exists():
        return False, None
    if not target.is_file():
        return False, f"知识文档不是常规文件： {target}"
    try:
        return bool(target.read_text(encoding="utf-8").strip()), None
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"知识文档不可读： {target}: {exc}"


# 仅领域路由或模块文档能够建立正式领域，散落文档不自动升级身份。
def _formal_domain_directories(paths: ModusPaths, documents: set[Path]) -> list[Path]:
    """Return domains with a routing Skill or at least one Module document."""

    if not paths.domain_dir.is_dir() or paths.domain_dir.is_symlink():
        return []
    formal: list[Path] = []
    for domain in sorted(paths.domain_dir.iterdir(), key=lambda item: item.name):
        if domain.is_symlink() or not domain.is_dir():
            continue
        module_dir = domain / "references" / "modules"
        has_module = any(target.parent == module_dir for target in documents)
        if domain / "SKILL.md" in documents or has_module:
            formal.append(domain)
    return formal


# 必需导航产物统一从共享种类表展开，避免各成员使用不同完整性标准。
def _required_domain_documents(domains: Sequence[Path]) -> list[Path]:
    """Expand formal domains into their complete generated navigation contract."""

    required: list[Path] = []
    for domain in domains:
        required.append(domain / "SKILL.md")
        required.extend(
            domain / "references" / f"{kind}.md" for kind in DOMAIN_GENERATED_REFERENCE_KINDS
        )
    return required


# 从实际文档推导完整性，不以安装清单替代知识证据。
# 缺失清单与读取问题分别保留，便于后续针对性补齐。
def _canonical_knowledge_observation(
    member: Mapping[str, Any], paths: ModusPaths
) -> tuple[str | None, list[str], list[str], list[str]]:
    documents, issues = _scan_markdown_payloads(paths.root, paths.domain_dir)
    document_set = set(documents)
    evidence: list[str] = []
    missing: list[str] = []
    formal_domains = _formal_domain_directories(paths, document_set)

    if not formal_domains:
        state = "partial" if documents else "unavailable" if issues else None
        return (
            state,
            [_workspace_evidence_path(member, paths, target) for target in documents],
            [],
            issues,
        )

    overview_available, overview_issue = _read_nonempty_markdown(
        paths.root, paths.architecture_overview
    )
    if overview_issue:
        issues.append(overview_issue)
    if overview_available:
        evidence.append(_workspace_evidence_path(member, paths, paths.architecture_overview))
    else:
        missing.append(_workspace_evidence_path(member, paths, paths.architecture_overview))

    for target in _required_domain_documents(formal_domains):
        if target in document_set:
            evidence.append(_workspace_evidence_path(member, paths, target))
        else:
            missing.append(_workspace_evidence_path(member, paths, target))
    for target in documents:
        relative = _workspace_evidence_path(member, paths, target)
        if relative not in evidence:
            evidence.append(relative)
    state = "partial" if missing or issues else "available"
    return state, evidence, missing, issues


# 先重新校验成员边界，再读取其安装和知识状态。
# 单个成员不可用时返回诊断卡片，保留其他成员的盘点结果。
def _inspect_member_knowledge(root: Path, member: WorkspaceMember) -> JsonObject:
    member_id = str(member.get("id") or "")
    member_path = str(member.get("path") or "")
    base: JsonObject = {
        "id": member_id,
        "path": member_path,
        "group": _member_group(member_path),
        "installed": False,
        "knowledge": {
            "state": "unavailable",
            "evidencePaths": [],
            "missingPaths": [],
            "reason": "成员目录不可用",
        },
    }
    try:
        validate_adoptable_member(root, member)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        base["knowledge"]["reason"] = str(exc)
        return base

    paths = ModusPaths.for_root(root / member_path)
    installed, install_issue = _installation_observation(paths)
    canonical, canonical_evidence, canonical_missing, canonical_issues = (
        _canonical_knowledge_observation(member, paths)
    )
    evidence = sorted(set(canonical_evidence))
    missing = sorted(set(canonical_missing))
    issues = canonical_issues
    state = canonical or ("unavailable" if issues else "missing")
    reason_parts = [*issues]
    if install_issue:
        reason_parts.append(f"安装清单： {install_issue}")
    return {
        **base,
        "installed": installed,
        "installIssue": install_issue,
        "knowledge": {
            "state": state,
            "evidencePaths": evidence,
            "missingPaths": missing,
            "reason": "; ".join(reason_parts) if reason_parts else None,
        },
    }


# 统计基于同一批瞬时观察，避免数量与成员明细使用不同快照。
def inspect_workspace_knowledge(
    root: str | Path, *, members: Iterable[WorkspaceMember] | None = None
) -> JsonObject:
    """Return a transient member knowledge inventory for an explicit workspace."""
    paths = ModusPaths.for_root(root)
    config = ModusConfig.read(paths)
    if config.kind != "workspace":
        raise ValueError("工作区知识盘点需要 kind: workspace")
    selected = list(config.workspace.get("members", ())) if members is None else list(members)
    records = [_inspect_member_knowledge(paths.root, member) for member in selected]
    # 缺少知识与成员不可读取分开统计，后续处理方式不同。
    states = ("available", "partial", "missing", "unavailable")
    counts = {
        state: sum(item["knowledge"]["state"] == state for item in records) for state in states
    }
    counts.update(
        total=len(records),
        installed=sum(bool(item["installed"]) for item in records),
        uninstalled=sum(not item["installed"] for item in records),
    )
    return {"counts": counts, "members": records}


# 复用打包模板作为路由说明的唯一来源，防止安装文案分叉。
def handbook_skill_text() -> str:
    """Read the canonical workspace routing Skill without a second prose copy."""
    from .scaffold.catalog import TemplateCatalog

    return TemplateCatalog().read("knowledge/handbook/SKILL.md")


# 工作区只拥有自己的入口文档，成员业务知识保持独立所有权。
def workspace_seed_files(paths: ModusPaths) -> dict[Path, str]:
    """Managed workspace entry point. Business folders remain user-owned."""
    return {paths.workspace_handbook_skill: handbook_skill_text()}


# 配置可能在初始化后变化，因此跨根读取前仍需验证路径。
# 禁止把工作区自身作为成员，防止路由递归回自身。
def validate_adoptable_member(root: str | Path, member: WorkspaceMember) -> JsonObject:
    """Validate a configured member immediately before any cross-root read."""

    base = Path(root).resolve()
    relative = Path(str(member["path"]))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"成员路径不规范： {member['path']}")
    raw_target = base / relative
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"成员路径包含符号链接： {member['path']}")
    target = raw_target.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"成员路径越出工作区： {member['path']}") from exc
    if target == base or not target.is_dir():
        raise ValueError(f"成员目录不可用： {member['path']}")
    if not ((target / ".git").is_dir() or (target / ".git").is_file()):
        raise ValueError(f"成员不是 Git 仓库或工作树根目录： {member['path']}")
    return dict(member)


# 投影从成员当前产品生成，可随时重建而无需回写成员。
# 源码入口取有界样本，避免成员卡片膨胀为完整文件清单。
def _member_projection(root: str | Path, member: WorkspaceMember) -> JsonObject:
    """Build one transient member card from its current knowledge and source."""

    from .scope_resolver import read_scope_products

    paths = ModusPaths.for_root(Path(root) / member["path"])
    products = read_scope_products(paths)
    domains = [item["name"] for item in products["domains"]]
    owned_modules: dict[str, list[str]] = {}
    for module in products.get("module-catalog", {}).get("modules", ()):
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("moduleId") or "")
        parent = str(module.get("parent") or module_id.partition("/")[0])
        if module_id and parent:
            owned_modules.setdefault(parent, []).append(module_id)
    from .retrieval.index import markdown_files
    from .source_listing import list_source_paths

    entries = [
        f"{member['path']}/{path.relative_to(paths.root).as_posix()}"
        for path in markdown_files(paths, "project")
    ]
    sources = [
        f"{member['path']}/{path.relative_to(paths.root).as_posix()}"
        for relative in list_source_paths(paths.root)[:40]
        for path in (paths.root / relative,)
    ]
    knowledge = _inspect_member_knowledge(Path(root), member)["knowledge"]
    return {
        **member,
        "repositoryId": member.get("repositoryId") or member["id"],
        "domains": domains,
        "knowledge_entries": entries,
        "sourceEntries": sources,
        "knowledge": knowledge,
        "ownedModules": {
            domain: sorted(set(module_ids)) for domain, module_ids in owned_modules.items()
        },
    }


# 所有成员共享请求预算，避免成员数量放大总耗时。
# 路径先转为成员相对路径，成员失败以独立错误返回。
# 结果携带请求摘要，原始请求不用于持久身份。
def resolve_context(
    root: str | Path,
    request: str,
    *,
    scope: str,
    explicit_paths: Iterable[str] | None = None,
    purpose: str | None = None,
    budget: RetrievalBudget | None = None,
) -> JsonObject:
    """在每个选定的工作区成员中独立解析有预算限制的上下文。"""

    from .commands.context_cmd import resolve
    from .orchestrator.context_resolver import relative_paths
    from .retrieval.budget import RetrievalBudget
    from .retrieval.runtime import enrich_workspace_result, select_workspace_members
    from .retrieval.stages.settings import retrieval_settings

    paths = ModusPaths.for_root(root)
    if budget is None:
        settings = retrieval_settings(paths)
        budget = RetrievalBudget.start(
            float(settings.get("cold_budget_ms", 3000)),
            int(settings.get("max_context_chars", 12000)),
        )
    explicit_paths = relative_paths(paths, explicit_paths)
    members, route = select_workspace_members(paths, request, explicit_paths=explicit_paths)
    route["paths"] = explicit_paths
    results: list[JsonObject] = []
    errors: list[JsonObject] = []
    for member in members:
        # 预算耗尽的成员仍进入错误明细，使调用方知道哪些成员未被查询。
        if budget.exhausted:
            errors.append({"memberId": member["id"], "reason": "request-deadline"})
            continue
        try:
            member_root = (paths.root / member["path"]).resolve()
            member_root.relative_to(paths.root)
            if member_root == paths.root or not member_root.is_dir():
                raise ValueError("成员目录不可用")
            selected_paths = [
                Path(p).relative_to(member["path"]).as_posix()
                for p in explicit_paths or ()
                if p == member["path"] or p.startswith(member["path"] + "/")
            ]
            context = resolve(
                str(member_root),
                request,
                scope=scope,
                explicit_paths=selected_paths,
                purpose=purpose,
                budget=budget,
            )
            results.append(
                {"memberId": member["id"], "memberPath": member["path"], "context": context}
            )
        except (OSError, ValueError) as exc:
            errors.append({"memberId": member["id"], "reason": str(exc)})
    base = {
        "kind": "workspace",
        "scope": scope,
        "requestHash": hashlib.sha256(request.encode()).hexdigest(),
        "memberContexts": results,
        "unavailableMembers": errors,
        "sourceFiles": [],
        "knowledgeFiles": [],
        "freshness": {},
    }
    return enrich_workspace_result(
        paths.root,
        request,
        base,
        route=route,
        purpose=purpose,
        budget=budget,
    )


# 成员失败时仍生成带原因的卡片，地图不会静默遗漏该成员。
# 写入使用现有内容摘要，检测读取与替换之间的并发修改。
def refresh_generated(root: str | Path) -> JsonObject:
    """Rebuild workspace maps from live, read-only member projections."""

    paths = ModusPaths.for_root(root)
    config = ModusConfig.read(paths)
    if config.kind != "workspace":
        raise ValueError("工作区地图刷新需要 kind: workspace")
    members: list[JsonObject] = []
    errors: list[JsonObject] = []
    for member in config.workspace.get("members", ()):
        try:
            validate_adoptable_member(paths.root, member)
            members.append(_member_projection(paths.root, member))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append({"memberId": member["id"], "reason": str(exc)})
            members.append(
                {
                    **member,
                    "repositoryId": member.get("repositoryId") or member["id"],
                    "domains": [],
                    "knowledge_entries": [],
                    "knowledge": {"state": "unavailable", "reason": str(exc)},
                    "ownedModules": {},
                }
            )
    maps = _render_maps(members)
    written: list[str] = []
    maps.update(
        {f"members/{member['repositoryId']}": _member_markdown(member) for member in members}
    )
    for name, content in maps.items():
        target = paths.knowledge_dir / "handbook" / "references" / "generated" / f"{name}.md"
        from .knowledge_reader import BoundKnowledgeReadError, read_bound_regular_bytes
        from .knowledge_write_session import write_document

        try:
            before = read_bound_regular_bytes(paths.root, target)
            digest = hashlib.sha256(before).hexdigest()
        except BoundKnowledgeReadError as exc:
            # 只有目标缺失允许按新文档创建，其他读取异常必须中止写入。
            if exc.kind != "missing":
                raise
            digest = None
        write_document(paths, paths.relative(target), content, expected_hash=digest)
        written.append(paths.relative(target))
    return {
        "status": "partial" if errors else "completed",
        "writes": written,
        "unavailableMembers": errors,
    }


# 创建范围限制在工作区知识根，成员目录不由此服务管理。
def ensure_workspace_directories(paths: ModusPaths) -> None:
    """Create only the workspace-owned knowledge root."""

    paths.knowledge_dir.mkdir(parents=True, exist_ok=True)


# 按路径前缀把变更分派给成员，未分派项由工作区本地处理。
# 成员结果重新加前缀，保证候选路径在工作区内可直接定位。
# 此服务只返回影响候选，知识正文仍由后续创作流程决定。
def sync_knowledge(
    root: str | Path,
    changed_files: Iterable[str] = (),
    *,
    targets: Iterable[str] = (),
    validation: str = "passed",
) -> JsonObject:
    """Route read-only knowledge-impact candidates to their owning members."""

    from .orchestrator.context_resolver import relative_paths
    from .orchestrator.knowledge_sync import sync_knowledge as sync_member

    paths = ModusPaths.for_root(root)
    changed_files = relative_paths(paths, changed_files)
    config = ModusConfig.read_available(paths)
    results: list[JsonObject] = []
    # 已分派集合用于扣除工作区本地变更，避免同一路径被重复检查。
    assigned: set[str] = set()
    routed_targets: set[str] = set()
    for member in config.workspace.get("members", ()):
        prefix = member["path"] + "/"
        changed = [p for p in changed_files if p.startswith(prefix)]
        member_targets = [p for p in targets if p.startswith(prefix)]
        if not changed and not member_targets:
            continue
        assigned.update(changed)
        routed_targets.update(member_targets)
        try:
            validate_adoptable_member(paths.root, member)
            result = sync_member(
                str(paths.root / member["path"]),
                [p[len(prefix) :] for p in changed],
                targets=[p[len(prefix) :] for p in member_targets],
                validation=validation,
                _local=True,
            )
            results.append({"memberId": member["id"], "result": result})
        except (OSError, ValueError, RuntimeError) as exc:
            results.append({"memberId": member["id"], "error": str(exc)})
    local = sync_member(
        str(root),
        [p for p in changed_files if p not in assigned],
        targets=[p for p in targets if p not in routed_targets],
        validation=validation,
        _local=True,
    )
    # 任何成员冲突都使整体状态降为部分完成，同时保留各成员明细。
    partial = local["status"] == "partial" or any(
        i.get("error") or i.get("result", {}).get("status") == "partial" for i in results
    )
    member_paths = {member["id"]: member["path"] for member in config.workspace.get("members", ())}
    candidates = list(local.get("candidates", []))
    for item in results:
        member_result = item.get("result") or {}
        prefix = member_paths.get(item["memberId"], "")
        for candidate in member_result.get("candidates", []):
            candidates.append(
                {
                    **candidate,
                    "path": (Path(prefix) / candidate["path"]).as_posix(),
                    "memberId": item["memberId"],
                }
            )
    return {
        "status": "partial" if partial else "noop",
        "mode": "agent-authored",
        "members": results,
        "workspace": local,
        "candidates": candidates,
        "lowConfidenceCandidates": [
            item["path"] for item in candidates if item.get("confidence") == "path-only"
        ],
        "unavailableMembers": [
            {"memberId": item["memberId"], "reason": item["error"]}
            for item in results
            if item.get("error")
        ],
        "writes": [],
        "skipped": local.get("skipped", []),
        "conflicts": [
            *local.get("conflicts", []),
            *[
                {"memberId": item["memberId"], "reason": item["error"]}
                for item in results
                if item.get("error")
            ],
        ],
    }


__all__ = [
    "MAP_NAMES",
    "discover_members",
    "ensure_workspace_directories",
    "handbook_skill_text",
    "inspect_workspace_knowledge",
    "normalize_members",
    "refresh_generated",
    "resolve_context",
    "sync_knowledge",
    "validate_adoptable_member",
    "workspace_seed_files",
]
