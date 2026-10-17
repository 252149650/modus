"""File-local syntax observations for the Modus analysis contract.

This module only reads its source argument. Project configuration, filesystem
lookups and cross-file identity decisions belong to native_binding. A cached
FileFacts value contains no tree-sitter objects and no repository-wide state.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable

from modus.machine.corpus.source_snapshot import SourcePath, parse_source
from modus.machine.facts import native_syntax
from modus.machine.facts.native_syntax import (
    _GRAMMARS,
    PARSER_CONTRACT_VERSION,
    component_script_bytes,
    file_identity,
)
from modus.machine.facts.native_visitor import _FileParser
from modus.machine.store.ids import make_id

# Public parser contracts remain available at their original import location.
FileFacts = native_syntax.FileFacts
Extractor = native_syntax.Extractor
REFERENCE_CONTEXTS = native_syntax.REFERENCE_CONTEXTS


# 组件模板仅遮罩非脚本字节，访问器仍从原字节提取文本与摘要。
# 语法错误产生 partial 观察，不把可恢复语法树当作完整编译结果。
def parse_file(path: SourcePath, language: str | None = None) -> dict:
    from tree_sitter import Language, Parser

    from modus.machine.facts.language_capabilities import capability_for

    language = language or capability_for(str(path)).language
    if path.suffix in {".ts", ".mts", ".cts"}:
        language = "typescript"
    raw = path.read_bytes()
    masked = raw
    if path.suffix in {".vue", ".svelte", ".astro"}:
        masked, typed = component_script_bytes(path, astro=path.suffix == ".astro")
        language = "typescript" if typed else "javascript"
    if path.suffix == ".tsx":
        language = "tsx"
    module_name, factory = _GRAMMARS[language]
    module = importlib.import_module("tree_sitter_" + module_name)
    parser = Parser(Language(getattr(module, factory)()))
    tree = parse_source(path, parser, masked)
    visitor = _FileParser(path, language, raw)
    visitor.walk(tree.root_node, visitor.root_scope)
    return dict(visitor.result(tree.root_node.has_error))


# 注册表绑定语言但不持有解析器状态，每个文件拥有独立访问器。
def parser_for(language: str) -> Callable[[SourcePath], dict]:
    def extract(path: SourcePath) -> dict:
        return parse_file(path, language)

    return extract


# 标题只形成文档包含关系，不能当成程序声明或调用证据。
# 入口保持在原模块，现有注册表和消费者无需随访问器拆分改名。
def extract_markdown(path: SourcePath) -> dict:
    """Document headings are deterministic source sections, not semantic code."""
    raw = path.read_bytes()
    parser = _FileParser(path, "markdown", raw)
    for line, text in enumerate(raw.decode("utf-8").splitlines(), 1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", text)
        if not match:
            continue
        identity = make_id(parser.prefix, "section", str(line), match[2])
        parser.nodes.append(
            {
                "id": identity,
                "label": match[2],
                "kind": "section",
                "file_type": "document",
                "source_file": parser.relative,
                "source_location": f"L{line}:1",
                "heading_level": len(match[1]),
            }
        )
        parser.edges.append(
            {
                "source": parser.file_id,
                "target": identity,
                "relation": "contains",
                "resolution": "exact",
                "source_file": parser.relative,
                "source_location": f"L{line}:1",
            }
        )
    return dict(parser.result(False))


__all__ = [
    "PARSER_CONTRACT_VERSION",
    "REFERENCE_CONTEXTS",
    "FileFacts",
    "Extractor",
    "component_script_bytes",
    "file_identity",
    "parse_file",
    "parser_for",
    "extract_markdown",
]
