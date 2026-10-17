"""Source vocabulary used by initialization capability evidence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from modus.machine.facts.source_contracts import _annotations, _http_contract


# 标签来自源码已有的名称与契约，不在此处推断业务含义。
# Java 注解中的路由与摘要补充检索词，原注释单独返回。
# 去重保留首次出现顺序，使后续评分和展示具有稳定输入。
def source_vocabulary(attrs: dict[str, Any]) -> tuple[list[str], str]:
    labels = [
        str(attrs.get(key) or "")
        for key in ("label", "qualified_name", "symbol", "api", "route", "display_name")
    ]
    annotations = _annotations(attrs)
    if annotations and (
        attrs.get("language") == "java" or Path(str(attrs.get("source_file", ""))).suffix == ".java"
    ):
        api, route = _http_contract(
            annotations, symbol=attrs.get("qualified_name"), kind=attrs.get("kind", "method")
        )
        labels.extend([api or "", route or ""])
        for annotation in annotations:
            labels.extend(
                re.findall(r'(?:summary|description|name|apiName)\s*=\s*"([^"\n]+)"', annotation)
            )
    labels.extend(
        contract["identity"] for contract in attrs.get("contracts", []) if contract.get("identity")
    )
    comment = str(attrs.get("doc_comment") or attrs.get("comment") or "")
    return list(dict.fromkeys(label for label in labels if label)), comment
