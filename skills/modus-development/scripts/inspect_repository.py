#!/usr/bin/env python3
"""只读核对 Modus checkout 与 skill 的源码基线，不导入 Modus。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit(
        "请使用 Python 3.11+ 运行，例如 python3.11 inspect_repository.py --root <Modus仓库>"
    ) from None


BASELINE = Path(__file__).resolve().parents[1] / "references" / "source-baseline.json"


def git(root: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def digest(path: Path) -> str | None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def literal_assignments(path: Path) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                values[node.target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
    return values


def inspect(root: Path) -> dict[str, object]:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project.get("name") != "openmodus" or not (root / "src/modus/cli.py").is_file():
        raise ValueError("目标不是本技能对应的 openmodus Python 源码仓库")
    top = git(root, "rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != root:
        raise ValueError("请指定 Modus Git 仓库根目录")

    tracked = set((git(root, "ls-files") or "").splitlines())
    entries = baseline["files"]
    known = {entry["path"] for entry in entries}
    changed = [entry for entry in entries if digest(root / entry["path"]) != entry["sha256"]]
    added = sorted(tracked - known)
    untracked_source = sorted(
        path
        for path in (git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines()
        if path.startswith(("src/", "tests/", "scripts/", "schemas/", ".github/"))
    )
    missing = sorted(entry["path"] for entry in changed if not (root / entry["path"]).exists())
    modified = sorted(entry["path"] for entry in changed if entry["path"] not in missing)
    guides = Counter(entry["guide"] for entry in changed)
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "branch", "--show-current") or "(detached)"
    package_version = literal_assignments(root / "src/modus/__init__.py").get("__version__")
    surface = literal_assignments(root / "src/modus/surface.py")
    upstream = git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    warnings: list[str] = []
    if branch != baseline["branch"]:
        warnings.append("当前分支不同于知识基线；保持用户选择，回读相关模块")
    if head != baseline["commit"]:
        warnings.append("HEAD 与快照不同；文件漂移清单用于定位需要重读的所有者")
    if project.get("version") != package_version:
        warnings.append("pyproject 与包根版本不一致")
    if changed or added or untracked_source:
        warnings.append("源码树有基线漂移；指南不应直接当作当前实现事实")

    return {
        "root": str(root),
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "aheadBehind": git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        if upstream
        else None,
        "workingTree": (git(root, "status", "--short") or "").splitlines(),
        "package": {
            "name": project["name"],
            "version": project["version"],
            "sourceVersion": package_version,
        },
        "workflows": {
            key: surface.get(key) for key in ("CORE_SKILLS", "EXPLICIT_SKILLS", "AUTOMATIC_SKILLS")
        },
        "baseline": {key: baseline[key] for key in ("branch", "commit", "version", "reviewedOn")},
        "drift": {
            "baselineFileCount": len(entries),
            "matchingFileCount": len(entries) - len(changed),
            "modifiedCount": len(modified),
            "missingCount": len(missing),
            "newTrackedCount": len(added),
            "untrackedSourceCount": len(untracked_source),
            "modified": modified[:100],
            "missing": missing[:100],
            "newTracked": added[:100],
            "untrackedSource": untracked_source[:100],
            "refreshGuides": dict(sorted(guides.items())),
            "newTrackedGuide": "references/global-overview.md" if added else None,
            "pathListsLimitedTo": 100,
        },
        "warnings": warnings,
        "remoteFetched": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Modus Git 源码根目录")
    args = parser.parse_args()
    try:
        result = inspect(args.root.expanduser().resolve())
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
