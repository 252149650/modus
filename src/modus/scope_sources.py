"""Read explicitly authorized classification material without running analysis."""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker

from .knowledge_reader import BoundKnowledgeReadError, read_bound_regular_bytes
from .schema_resources import load_schema
from .scope_models import ScopeRef

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 5000
MAX_COLUMNS = 128
MAX_CELL = 2000
MAX_XML_BYTES = 32 * 1024 * 1024


class ScopeSourceError(ValueError):
    """A material failure with no cell contents or credentials in its message."""


@dataclass(frozen=True)
class SourceRecord:
    record_id: str
    label: str
    group: str
    level: str
    locator: dict[str, Any]
    declared_id: str | None = None
    parent_id: str | None = None
    scope_ref: str | None = None
    platforms: tuple[str, ...] = ()
    platform_origin: str = "unspecified"


@dataclass
class Material:
    source_id: str
    records: list[SourceRecord] = field(default_factory=list)
    digest: str | None = None
    error: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


def normalized_label(value: str) -> str:
    """Normalize whitespace, never the spelling or case of a stable identity."""
    return " ".join(value.split())


def validate_payload(data: object, definition: str | None = None) -> None:
    schema = load_schema("scope-sources.schema.yaml")
    if definition:
        schema = {"$ref": f"#/definitions/{definition}", "definitions": schema["definitions"]}
    error = next(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(data), None)
    if error:
        # jsonschema messages may echo confidential instance values.
        raise ScopeSourceError(f"输入结构不合法（校验规则：{error.validator}）")


def read_material_bytes(root: Path, path: str, allowed_roots: tuple[Path, ...] = ()) -> bytes:
    if any(ord(char) < 32 for char in path):
        raise ScopeSourceError("来源路径不能包含控制字符")
    if "://" in path or "\\" in path or ".." in Path(path).parts:
        raise ScopeSourceError("来源路径必须为明确的本地路径，不接受URL或上级跳转")
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    for boundary in (root, *allowed_roots):
        if target.is_relative_to(boundary):
            try:
                return read_bound_regular_bytes(boundary, target, max_bytes=MAX_BYTES)
            except (BoundKnowledgeReadError, OSError) as exc:
                raise ScopeSourceError("来源不可读、越界、非普通文件或超过大小限制") from exc
    raise ScopeSourceError("来源位于授权读取根之外；需由调用者显式传入允许根")


def load_input(
    root: Path, path: str, allowed_roots: tuple[Path, ...]
) -> tuple[dict[str, Any], str]:
    raw = read_material_bytes(root, path, allowed_roots)
    try:

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ScopeSourceError("输入JSON存在重复字段")
                result[key] = value
            return result

        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ScopeSourceError("无法解析来源清单JSON") from exc
    validate_payload(data)
    ids = [source["id"] for source in data["sources"]]
    if len(ids) != len(set(ids)):
        raise ScopeSourceError("来源ID重复")
    return data, hashlib.sha256(raw).hexdigest()


def _text(value: object) -> str:
    if value is None:
        return ""
    if (
        not isinstance(value, str)
        or len(value) > MAX_CELL
        or any(ord(c) < 32 and c not in "\t\n\r" for c in value)
    ):
        raise ScopeSourceError("分类单元格须为有界文本")
    return value


def _platforms(text: str, defaults: tuple[str, ...]) -> tuple[str, tuple[str, ...], str]:
    match = re.search(r"\s*\[platform=([^\]]+)\]\s*$", text)
    if not match:
        return text, defaults, "source-default" if defaults else "unspecified"
    values = tuple(match[1].split(","))
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", v) for v in values) or len(
        set(values)
    ) != len(values):
        raise ScopeSourceError("平台声明格式不合法")
    return text[: match.start()].rstrip(), values, "explicit"


def parse_mindmap(raw: bytes, spec: dict[str, Any]) -> list[SourceRecord]:
    """Parse numbered H2 domains, H3 IDs and single-level bullet scenarios."""
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise ScopeSourceError("分类树不是UTF-8文本") from exc
    start, end = 0, len(lines)
    if "startMarker" in spec:
        starts = [i for i, line in enumerate(lines) if line.strip() == spec["startMarker"]]
        ends = [i for i, line in enumerate(lines) if line.strip() == spec["endMarker"]]
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ScopeSourceError("分类树边界标记缺失、重复或顺序错误")
        start, end = starts[0] + 1, ends[0]
    defaults = tuple(spec.get("defaultPlatforms", ()))
    records: list[SourceRecord] = []
    domain_id = domain_label = module_id = ""
    seen: set[str] = set()
    fence: str | None = None
    for number in range(start, end):
        line = lines[number]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence:
            continue
        heading = re.fullmatch(r"##\s+\d+\.\s+(.+)", line)
        module = re.fullmatch(r"###\s+(\S+)\s+(.+)", line)
        child = re.fullmatch(r"-\s+([A-Za-z][A-Za-z0-9_-]*)(?:\s+|[:：]\s*)(.+)", line)
        if heading:
            value, platforms, origin = _platforms(heading[1], defaults)
            try:
                label, identifier = value.rsplit(None, 1)
                ref = str(ScopeRef.for_domain(identifier))
            except ValueError as exc:
                raise ScopeSourceError("分类树H2应为编号、标题和稳定ID") from exc
            domain_id, domain_label, module_id = identifier, label, ""
            level, parent = "domain", None
        elif module:
            if not domain_id:
                raise ScopeSourceError("模块没有所属大类")
            identifier = module[1]
            label, platforms, origin = _platforms(module[2], defaults)
            try:
                ref = str(ScopeRef.for_module(domain_id, identifier))
            except ValueError as exc:
                raise ScopeSourceError("模块稳定ID不合法") from exc
            module_id = identifier
            level, parent = "module", domain_id
        elif child and module_id:
            identifier = child[1]
            label, platforms, origin = _platforms(child[2], defaults)
            label = re.split(r"[:：]", label, maxsplit=1)[0]
            ref = None
            level, parent = "scenario", module_id
        elif line.startswith(("## ", "### ")):
            raise ScopeSourceError("分类树存在无法解析的标题；请显式限定模块区间")
        else:
            continue
        if identifier in seen:
            raise ScopeSourceError("分类树稳定ID重复")
        seen.add(identifier)
        records.append(
            SourceRecord(
                f"node:{identifier}",
                _text(label),
                domain_label,
                level,
                {"line": number + 1},
                identifier,
                parent,
                ref,
                platforms,
                origin,
            )
        )
        if len(records) > MAX_RECORDS:
            raise ScopeSourceError("来源记录超过上限")
    if fence or not records:
        raise ScopeSourceError("分类树围栏不完整或指定范围没有节点")
    return records


def _table_records(rows: Any, spec: dict[str, Any]) -> list[SourceRecord]:
    columns = spec["columns"]
    if len(set(columns.values())) != len(columns):
        raise ScopeSourceError("列映射不能重复使用同一列")
    try:
        header = next(rows)
    except StopIteration as exc:
        raise ScopeSourceError("表格没有表头") from exc
    if len(header) > MAX_COLUMNS:
        raise ScopeSourceError("表格列数超过上限")
    positions: dict[str, int] = {}
    for key, name in columns.items():
        matches = [i for i, value in enumerate(header) if value == name]
        if len(matches) != 1:
            raise ScopeSourceError("所选列缺失或表头重名")
        positions[key] = matches[0]
    records: list[SourceRecord] = []
    seen: set[str] = set()
    for number, row in enumerate(rows, 2):
        if number > MAX_RECORDS + 1 or len(row) > MAX_COLUMNS:
            raise ScopeSourceError("表格行列数超过上限")
        values = {key: _text(row[i] if i < len(row) else None) for key, i in positions.items()}
        if not any(v.strip() for v in values.values()):
            continue
        if not values["group"].strip() or not values["label"].strip():
            raise ScopeSourceError("所选分类行缺少大类或模块；不自动向下填充")
        record_id = f"id:{values['id']}" if values.get("id", "").strip() else f"row:{number}"
        if record_id in seen:
            raise ScopeSourceError("表格记录ID重复")
        seen.add(record_id)
        records.append(
            SourceRecord(
                record_id,
                values["label"],
                values["group"],
                "module",
                {
                    "row": number,
                    "columns": columns,
                    **({"sheet": spec["sheet"]} if "sheet" in spec else {}),
                },
                platforms=tuple(spec.get("defaultPlatforms", ())),
                platform_origin="source-default" if spec.get("defaultPlatforms") else "unspecified",
            )
        )
    if not records:
        raise ScopeSourceError("所选列没有分类记录")
    return records


def _validate_sheet_xml(content: bytes) -> None:
    """Bound actual and implicit coordinates before openpyxl allocates row objects."""
    row_count = row_index = column_index = cells = 0
    in_row = False
    first = True
    try:
        for event, element in ET.iterparse(io.BytesIO(content), events=("start", "end")):
            tag = element.tag.rsplit("}", 1)[-1]
            if first:
                first = False
                if tag != "worksheet":
                    return
            if event == "start" and tag == "row":
                if in_row:
                    raise ScopeSourceError("工作表行结构不合法")
                in_row = True
                row_count += 1
                raw_index = element.get("r", str(row_index + 1))
                if not raw_index.isdecimal() or len(raw_index) > 6:
                    raise ScopeSourceError("表格行数超过上限")
                next_index = int(raw_index)
                if not row_index < next_index <= MAX_RECORDS + 1 or row_count > MAX_RECORDS + 1:
                    raise ScopeSourceError("表格行数超过上限或行顺序异常")
                row_index, column_index, cells = next_index, 0, 0
            elif event == "start" and tag == "c":
                if not in_row:
                    raise ScopeSourceError("工作表单元格不属于行")
                cells += 1
                coordinate = element.get("r")
                column = column_index + 1
                if coordinate is not None:
                    match = re.fullmatch(r"([A-Z]{1,3})([0-9]{1,6})", coordinate)
                    if not match:
                        raise ScopeSourceError("工作表坐标不合法")
                    if int(match[2]) != row_index:
                        raise ScopeSourceError("工作表行与单元格坐标不一致")
                    column = 0
                    for letter in match[1]:
                        column = column * 26 + ord(letter) - ord("A") + 1
                if cells > MAX_COLUMNS or not column_index < column <= MAX_COLUMNS:
                    raise ScopeSourceError("表格列数超过上限或列顺序异常")
                column_index = column
            elif event == "end":
                if tag == "row":
                    in_row = False
                element.clear()
    except ET.ParseError as exc:
        raise ScopeSourceError("工作表XML不合法") from exc


def _xlsx_records(raw: bytes, spec: dict[str, Any]) -> list[SourceRecord]:
    # Bound decompression before the XML reader opens any workbook member.
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 1000 or len({m.filename for m in members}) != len(members):
                raise ScopeSourceError("工作簿压缩包成员异常")
            total = 0
            for member in members:
                if member.flag_bits & 1 or member.file_size > MAX_XML_BYTES:
                    raise ScopeSourceError("工作簿成员加密或超过限制")
                total += member.file_size
                if total > MAX_XML_BYTES:
                    raise ScopeSourceError("工作簿解压大小超过限制")
                with archive.open(member) as stream:
                    content = stream.read(MAX_XML_BYTES + 1)
                if len(content) != member.file_size:
                    raise ScopeSourceError("工作簿成员大小不一致")
                if member.filename.endswith((".xml", ".rels")):
                    try:
                        xml = content.decode("utf-8-sig")
                    except UnicodeError as exc:
                        raise ScopeSourceError("工作簿XML编码不受支持") from exc
                    if "\x00" in xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                        raise ScopeSourceError("工作簿不允许DTD、实体声明或非UTF-8 XML")
                    _validate_sheet_xml(content)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ScopeSourceError("工作簿压缩结构损坏") from exc
    try:
        openpyxl = importlib.import_module("openpyxl")
    except ImportError as exc:
        raise ScopeSourceError("读取XLSX需要显式安装 openmodus[materials]，不会自动安装") from exc
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(raw), read_only=True, data_only=False, keep_links=False
        )
        try:
            sheet = workbook[spec["sheet"]]
            sheet.reset_dimensions()
            rows = sheet.iter_rows(max_col=MAX_COLUMNS)
            header_cells = next(rows)
            header = tuple(cell.value for cell in header_cells)
            selected = [i for i, value in enumerate(header) if value in spec["columns"].values()]

            def values():
                yield header
                for row in rows:
                    if any(row[i].data_type == "f" for i in selected):
                        raise ScopeSourceError("所选分类列包含公式，不使用缓存值替代分类文本")
                    yield tuple(cell.value if i in selected else None for i, cell in enumerate(row))

            return _table_records(iter(values()), spec)
        finally:
            workbook.close()
    except ScopeSourceError:
        raise
    except Exception as exc:
        raise ScopeSourceError("无法读取指定工作表") from exc


def load_material(root: Path, spec: dict[str, Any], allowed_roots: tuple[Path, ...]) -> Material:
    material = Material(spec["id"], provenance=spec.get("provenance", {}))
    try:
        raw = read_material_bytes(root, spec["path"], allowed_roots)
        material.digest = hashlib.sha256(raw).hexdigest()
        if spec["format"] == "mindmap":
            material.records = parse_mindmap(raw, spec)
        elif spec["format"] == "csv":
            material.records = _table_records(
                iter(csv.reader(io.StringIO(raw.decode("utf-8-sig")))), spec
            )
        else:
            material.records = _xlsx_records(raw, spec)
    except (ScopeSourceError, UnicodeError, csv.Error) as exc:
        material.error = (
            str(exc) if isinstance(exc, ScopeSourceError) else "分类文本编码或CSV格式不合法"
        )
    return material
