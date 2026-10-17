"""Command-line orchestration for release smoke checks."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .archive import inspect_artifact
from .contracts import ArtifactKind, ReleaseCheckError
from .runtime import exercise_installed_wheel
from .verification import verify_artifact


# 参数只接收显式产物路径，不在 dist 中自动挑选可能陈旧的包。
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证 Modus wheel 与源码包，并执行安装后冒烟检查。"
    )
    parser.add_argument(
        "artifacts",
        metavar="ARTIFACT",
        nargs="+",
        type=Path,
        help=".whl 或 .tar.gz 安装包路径",
    )
    return parser


# 每个产物独立核验，wheel 额外执行安装后生命周期场景。
# 错误写入 stderr 并返回非零状态，适合直接作为发布流水线门禁。
# 成功信息由核验结果构造，不因归档能够打开就提前宣布通过。
def main(argv: Sequence[str] | None = None) -> int:
    """Validate every artifact, exercising each wheel after inspection."""

    arguments = _build_parser().parse_args(argv)
    try:
        for artifact in arguments.artifacts:
            inventory = inspect_artifact(artifact)
            artifact_result = verify_artifact(inventory)
            print(
                f"[通过] {artifact_result.path.name}: {artifact_result.member_count} 个条目；"
                f"{artifact_result.size_bytes:,} 字节；"
                f"{artifact_result.workflow_count} 个工作流；"
                "架构与发布边界完整"
            )
            if inventory.kind is ArtifactKind.WHEEL:
                runtime_result = exercise_installed_wheel(inventory.path)
                print(
                    f"[通过] 已安装 wheel：{runtime_result.workflow_count} 个工作流；"
                    f"{runtime_result.platform_count} 个平台；知识数据边界、"
                    "初始化分析、更新与卸载验证通过"
                )
    except ReleaseCheckError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    return 0
