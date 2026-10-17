"""Source-level annotation and HTTP contract extraction for initialization evidence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from modus.sensitive_literals import contains_sensitive_literal

_HTTP_ANNOTATION_METHODS: tuple[tuple[str, str], ...] = (
    ("OpenApiPostMapping", "POST"),
    ("OpenApiGetMapping", "GET"),
    ("OpenApiRequestMapping", "ANY"),
    ("PostMapping", "POST"),
    ("GetMapping", "GET"),
    ("PutMapping", "PUT"),
    ("DeleteMapping", "DELETE"),
    ("PatchMapping", "PATCH"),
    ("HeadMapping", "HEAD"),
    ("OptionsMapping", "OPTIONS"),
)

_JAVA_UNICODE_ESCAPE_RE = re.compile(r"\\u+([0-9a-fA-F]{4})")


# 只接纳非空字符串，数字或对象不能通过隐式 str 转换变成契约文本。
def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# 排序去重形成稳定词汇，并在离开源码观察边界前排除敏感字面量。
def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values: Iterable[Any] = (value,) if isinstance(value, str) else value
    normalized = (_text(item) for item in values)
    return tuple(
        sorted(
            {
                text
                for text in normalized
                if text is not None and not contains_sensitive_literal(text)
            }
        )
    )


# 仅还原 Java Unicode 转义，不执行通用 escape 解码或解释其他序列。
def _decode_java_unicode_escapes(value: str) -> str:
    """Decode compiler-emitted Java unicode escapes without interpreting others."""

    return _JAVA_UNICODE_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 16)),
        value,
    )


# 兼容顶层和 semantic_attributes 两种生产者形状，输出共用同一规范化。
# 先解码再进行敏感检测，防止转义形式绕过文本规则。
def _annotations(attrs: dict[str, Any]) -> tuple[str, ...]:
    """Read compiler annotations without coupling projection to one Provider."""

    values: list[Any] = []
    for source in (attrs, attrs.get("semantic_attributes")):
        if not isinstance(source, dict):
            continue
        raw = source.get("annotations")
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, (list, tuple, set, frozenset)):
            values.extend(raw)
    return _strings(
        _decode_java_unicode_escapes(value) if isinstance(value, str) else value for value in values
    )


# 限定名去掉包前缀后比较标记，但不使用子串匹配近似注解。
def _annotation_name(annotation: str) -> str:
    head = annotation.lstrip("@").partition("(")[0].strip()
    return head.rsplit(".", 1)[-1]


# 只提取显式字符串值；变量表达式的运行结果不在静态观察能力内。
def _annotation_value(annotations: tuple[str, ...], key: str) -> str:
    pattern = re.compile(rf"\b{re.escape(key)}\s*=\s*(?:\{{\s*)?[\"']([^\"']+)[\"']")
    for annotation in annotations:
        match = pattern.search(annotation)
        if match:
            return match.group(1).strip()
    return ""


# 方法级路由才形成入口身份，类级 RequestMapping 不能独自证明可调用端点。
# 路径按 apiUrl、path、value 的既有优先级读取，首个明确映射决定结果。
# 缺少路径时返回无契约，不用符号名虚构 URL。
# apiName 是展示标签，route 继续保留 HTTP 方法与规范化路径。
def _http_contract(
    annotations: tuple[str, ...],
    *,
    symbol: str | None,
    kind: str,
) -> tuple[str | None, str | None]:
    """Project an exact method-level HTTP identity from compiler annotations."""

    method = ""
    mapping_annotation = ""
    for annotation in annotations:
        name = _annotation_name(annotation)
        for marker, http_method in _HTTP_ANNOTATION_METHODS:
            if name == marker:
                method = http_method
                mapping_annotation = annotation
                break
        if method:
            break
        if name == "RequestMapping" and (kind == "method" or "#" in (symbol or "")):
            request_method = re.search(r"RequestMethod\.([A-Z]+)", annotation)
            method = request_method.group(1) if request_method else "ANY"
            mapping_annotation = annotation
            break
    if not method:
        return None, None

    path = ""
    for key in ("apiUrl", "path", "value"):
        path = _annotation_value((mapping_annotation,), key)
        if path:
            break
    if not path:
        direct = re.search(r"\(\s*(?:\{\s*)?[\"']([^\"']+)[\"']", mapping_annotation)
        path = direct.group(1).strip() if direct else ""
    if not path:
        return None, None
    normalized_path = "/" + path.strip().lstrip("/")
    route = f"{method} {normalized_path}"
    api_name = _annotation_value(annotations, "apiName")
    return api_name or route, route
