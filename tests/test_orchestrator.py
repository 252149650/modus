"""Focused contracts for orchestration boundaries and fallback safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from _support.sensitive_samples import SYNTHETIC_TOKEN, synthetic_source_assignment
from modus.orchestrator import fallback
from modus.orchestrator.models import FallbackResult
from modus.orchestrator.path_scope import PathOutsideRootError, relative_paths
from modus.paths import ModusPaths


# 相对路径和绝对路径可能指向同一证据，去重必须在规范化之后。
# 越界软链单独覆盖：仅检查输入字符串前缀无法守住项目边界。
def test_project_paths_are_canonical_unique_and_symlink_safe(tmp_path: Path) -> None:
    source = tmp_path / "src/service.py"
    source.parent.mkdir()
    source.write_text("def run(): pass\n", encoding="utf-8")

    paths = ModusPaths.for_root(tmp_path)
    assert relative_paths(paths, ["src/service.py", source]) == ["src/service.py"]

    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("external = True\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    link.symlink_to(outside)
    try:
        with pytest.raises(PathOutsideRootError, match="项目之外"):
            relative_paths(paths, [link])
    finally:
        outside.unlink()


# 模拟口令仅用于证明降级搜索仍执行脱敏，不对应真实账户。
# 同时布置范围内外的同名线索，避免空结果掩盖范围过滤失效。
def test_source_fallback_redacts_credentials_and_honors_scope(tmp_path: Path) -> None:
    allowed = tmp_path / "app/payment.py"
    allowed.parent.mkdir()
    allowed.write_text(
        synthetic_source_assignment("payment_token") + "\ndef settle_payment(): pass\n",
        encoding="utf-8",
    )
    excluded = tmp_path / "other/payment.py"
    excluded.parent.mkdir()
    excluded.write_text("def settle_payment_elsewhere(): pass\n", encoding="utf-8")

    result = fallback.search(
        tmp_path,
        "payment_token",
        prefer_rg=False,
        path_prefixes=["app"],
    )

    assert result.files == ["app/payment.py"]
    assert SYNTHETIC_TOKEN not in result.hits[0]["text"]
    assert "***REDACTED***" in result.hits[0]["text"]


# 指定但不存在的范围应产生空结果，不能解释为“搜索整个项目”。
# 这保护上游范围解析失败时的最小读取边界。
def test_invalid_scope_never_widens_source_fallback(tmp_path: Path) -> None:
    (tmp_path / "payment.py").write_text("def settle_payment(): pass\n", encoding="utf-8")

    result = fallback.search(
        tmp_path,
        "settle_payment",
        prefer_rg=False,
        path_prefixes=["missing-scope"],
    )

    assert result.hits == []


# 文件列表沿用首次命中的证据顺序，后续重复命中不改变优先级。
# 故意使用非字母顺序，防止无意排序改变检索结果。
def test_fallback_result_preserves_first_evidence_order() -> None:
    result = FallbackResult(
        terms=["payment"],
        hits=[
            {"file": "b.py", "line": 2, "text": "payment", "term": "payment"},
            {"file": "a.py", "line": 1, "text": "payment", "term": "payment"},
            {"file": "b.py", "line": 3, "text": "payment", "term": "payment"},
        ],
        engine="python",
    )

    assert result.files == ["b.py", "a.py"]
