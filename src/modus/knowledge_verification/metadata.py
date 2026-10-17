"""Validate optional Module metadata without constraining authored prose."""

from __future__ import annotations

from ..paths import ModusPaths
from ..scope_models import ModuleMeta, validate_module_meta
from ..skillmd import split_frontmatter
from .discovery import ModuleDocument
from .markdown_integrity import read_document
from .models import VerificationFinding, finding


# 路径身份优先于可选声明，frontmatter 不能借助自报 ID 改写模块归属。
def verify_module_metadata(
    paths: ModusPaths, modules: tuple[ModuleDocument, ...]
) -> list[VerificationFinding]:
    """Check declared identity and schema fields when frontmatter is present.

    The physical path already supplies a stable Module identity. Frontmatter is
    therefore optional; once a document declares it, malformed or conflicting
    identity data is objective damage and must fail closed.
    """

    findings: list[VerificationFinding] = []
    for module in modules:
        document = paths.relative(module.path)
        text = read_document(paths, module.path)
        if text is None:
            continue
        frontmatter, body = split_frontmatter(text)
        if not frontmatter:
            if text.startswith("---") and body == text:
                findings.append(
                    finding(
                        document,
                        "module-frontmatter-invalid",
                        "无法安全解析模块 frontmatter",
                    )
                )
            continue

        schema_data = {key: value for key, value in frontmatter.items() if key != "modus"}
        schema_errors = validate_module_meta(schema_data)
        findings.extend(
            finding(document, "module-frontmatter-invalid", error) for error in schema_errors
        )

        expected_id = f"{module.domain}/{module.path.stem}"
        declared_id = frontmatter.get("moduleId")
        if declared_id is not None and declared_id != expected_id:
            findings.append(
                finding(
                    document,
                    "module-identity-mismatch",
                    f"moduleId 必须为 {expected_id!r}",
                    expected=expected_id,
                    actual=declared_id,
                )
            )
        declared_parent = frontmatter.get("parent")
        if declared_parent is not None and declared_parent != module.domain:
            findings.append(
                finding(
                    document,
                    "module-parent-mismatch",
                    f"parent 必须为 {module.domain!r}",
                    expected=module.domain,
                    actual=declared_parent,
                )
            )
        if not schema_errors and declared_id == expected_id:
            try:
                ModuleMeta.from_dict(schema_data)
            except (TypeError, ValueError) as exc:
                findings.append(finding(document, "module-frontmatter-invalid", str(exc)))
    return findings


__all__ = ["verify_module_metadata"]
