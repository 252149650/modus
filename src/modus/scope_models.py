"""Current Domain/Module identities and optional authored metadata."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final, Literal, TypeAlias

from jsonschema import Draft7Validator

from .schema_resources import load_schema
from .sensitive_literals import contains_sensitive_literal

ModuleState: TypeAlias = Literal["observed", "proposed", "confirmed"]
ScopeRealization: TypeAlias = Literal["business-only", "implemented"]
ScopeKind: TypeAlias = Literal["domain", "module"]

MODULE_STATES: Final = frozenset({"observed", "proposed", "confirmed"})
SCOPE_REALIZATIONS: Final = frozenset({"business-only", "implemented"})
MODULE_META_SCHEMA: Final = "module-meta.schema.yaml"
MODULE_META_SCHEMA_VERSION: Final = 1
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_CONFIGURED_CONTRACT_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]*\$\{[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)?\}$"
)
_MAX_STABLE_ID_LENGTH = 256
_UNKNOWN_OWNER = frozenset({"unknown", "tbd", "unverified", "unset", "missing"})
_RESERVED_FLAT_MODULE_NAMES = frozenset(
    {"skill.md", "domain-module-map.md", "business", "code", "module", "story"}
)


# 身份片段在进入路径前校验，禁止分隔符、控制字符和敏感字面量。
# 保持输入原值，避免静默清洗产生身份碰撞。
def _segment(value: str, *, label: str) -> str:
    if isinstance(value, str) and contains_sensitive_literal(value):
        raise ValueError(f"{label} 包含禁止的敏感字面量")
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} 必须为非空规范路径片段")
    if value in {".", ".."} or "/" in value or "\\" in value or ":" in value:
        raise ValueError(f"{label} 必须为单个规范路径片段，实际为 {value!r}")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(f"{label} 不能包含空白或控制字符")
    return value


# 字符串不能作为可迭代序列被拆成字符；空集合与缺省值统一表示。
def _strings(values, *, label: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raise ValueError(f"{label} 必须为字符串序列")
    result = tuple(values)
    if any(
        not isinstance(item, str) or not item.strip() or item != item.strip() for item in result
    ):
        raise ValueError(f"{label} 只能包含不带首尾空白的非空字符串")
    return result


# 缓存的是已校验的契约，避免每条元数据重复构造验证器。
@lru_cache(maxsize=16)
def _schema_validator(name: str) -> Draft7Validator:
    schema = load_schema(name)
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


# 按路径和消息排序，使多项错误的展示与输入映射顺序无关。
def _schema_errors(name: str, data: dict[str, Any]) -> list[str]:
    validator = _schema_validator(name)
    errors = sorted(
        validator.iter_errors(data),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    return [f"{_schema_error_path(error.absolute_path)}: {error.message}" for error in errors]


# 数组索引与对象键采用不同表示，方便定位具体契约字段。
def _schema_error_path(parts: Iterable[object]) -> str:
    """Render a jsonschema path as a compact, actionable JSONPath."""
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


# 字段所有权来自 schema，扩展字段时无需维护第二份白名单。
@lru_cache(maxsize=1)
def module_meta_field_names() -> frozenset[str]:
    """Return the schema-owned frontmatter keys from the validated contract."""
    schema = _schema_validator(MODULE_META_SCHEMA).schema
    if not isinstance(schema, Mapping):
        raise RuntimeError(f"{MODULE_META_SCHEMA} 必须包含 schema 映射")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise RuntimeError(f"{MODULE_META_SCHEMA} 必须定义对象的 properties 映射")
    return frozenset(properties)


# 配置占位符是源码契约的一部分，验证后保留原始表达式。
def _contract_id(value: str, *, label: str) -> str:
    """Validate a stable contract anchor, including exact config placeholders.

    Runtime annotations commonly declare MQ resources through a property name
    plus a default value. Keeping that expression verbatim is important:
    replacing it with a sanitized token would sever Scope evidence from the
    source contract. The placeholder grammar remains deliberately narrow and
    bounded; arbitrary interpolation or whitespace is not accepted.
    """
    if (
        not isinstance(value, str)
        or len(value) > _MAX_STABLE_ID_LENGTH
        or (not _STABLE_ID_RE.fullmatch(value) and not _CONFIGURED_CONTRACT_ID_RE.fullmatch(value))
        or contains_sensitive_literal(value)
    ):
        raise ValueError(
            f"{label} 必须为规范稳定 ID 或精确的已配置属性"
            f"契约表达式，最大长度为 {_MAX_STABLE_ID_LENGTH} characters"
        )
    return value


# 拒绝重复而非悄悄去重，使创作错误能够在边界处暴露。
def _deterministic_strings(values, *, label: str) -> tuple[str, ...]:
    """Validate one ModuleMeta list and give it a stable canonical order."""
    result = _strings(values, label=label)
    if any(item != item.strip() for item in result):
        raise ValueError(f"{label} 的值不能包含首尾空白")
    # 唯一性错误在排序前报告，避免规范化过程隐藏重复创作输入。
    if len(set(result)) != len(result):
        raise ValueError(f"{label} 的值必须唯一")
    return tuple(sorted(result))


# 不可变值对象把物理目录身份集中校验，序列化固定为两段路径。
# 保留名称不能作为模块，避免与导航产物相互覆盖。
@dataclass(frozen=True, order=True, slots=True)
class ModuleId:
    """Physical module identity, serialized as exactly ``<domain>/<module>``."""

    domain: str
    module: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _segment(self.domain, label="domain"))
        module = _segment(self.module, label="module")
        if module.lower() in _RESERVED_FLAT_MODULE_NAMES:
            raise ValueError(f"模块名称被扁平范围布局保留： {module!r}")
        object.__setattr__(self, "module", module)

    @classmethod
    def parse(cls, value: str) -> ModuleId:
        if not isinstance(value, str) or value.count("/") != 1:
            raise ValueError("模块 ID 必须准确序列化为 <domain>/<module>")
        domain, module = value.split("/", 1)
        return cls(domain, module)

    def __str__(self) -> str:
        return f"{self.domain}/{self.module}"

    def to_wire(self) -> str:
        return str(self)


# 带种类的引用统一领域与模块入口，避免仅凭字符串形状猜测范围。
# 领域查询和模块查询共享同一套规范身份。
@dataclass(frozen=True, order=True, slots=True)
class ScopeRef:
    """Canonical reference to either a Domain or a Module scope."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind == "domain":
            canonical = _segment(self.value, label="domain")
        elif self.kind == "module":
            canonical = str(ModuleId.parse(self.value))
        else:
            raise ValueError("范围 kind 可选值为：domain、module")
        object.__setattr__(self, "value", canonical)

    @classmethod
    def for_domain(cls, domain: str) -> ScopeRef:
        return cls("domain", domain)

    @classmethod
    def for_module(cls, module_id_or_domain: ModuleId | str, module: str | None = None) -> ScopeRef:
        if module is not None:
            module_id = ModuleId(str(module_id_or_domain), module)
        elif isinstance(module_id_or_domain, ModuleId):
            module_id = module_id_or_domain
        else:
            module_id = ModuleId.parse(module_id_or_domain)
        return cls("module", str(module_id))

    @classmethod
    def parse(cls, value: str) -> ScopeRef:
        if not isinstance(value, str) or ":" not in value:
            raise ValueError("范围引用必须为 domain:<domain> 或 module:<domain>/<module>")
        kind, identifier = value.split(":", 1)
        return cls(kind, identifier)

    @property
    def domain(self) -> str:
        return self.value if self.kind == "domain" else ModuleId.parse(self.value).domain

    @property
    def module_id(self) -> ModuleId | None:
        return ModuleId.parse(self.value) if self.kind == "module" else None

    def to_wire(self) -> str:
        return f"{self.kind}:{self.value}"

    def __str__(self) -> str:
        return self.to_wire()


# 确认状态必须能够追溯操作者、时间和证据。
# 证据集合为空时拒绝构造，避免无来源的权威升级。
@dataclass(frozen=True, slots=True)
class ConfirmationProvenance:
    """Auditable authority used when a Module becomes confirmed."""

    mode: str
    confirmed_by: str
    confirmed_at: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"human", "heuristic"}:
            raise ValueError("模块确认模式必须为 human 或 heuristic")
        if not isinstance(self.confirmed_by, str) or not self.confirmed_by.strip():
            raise ValueError("confirmation 缺少 confirmed_by")
        if not isinstance(self.confirmed_at, str) or not self.confirmed_at.strip():
            raise ValueError("confirmation 缺少 confirmed_at")
        object.__setattr__(self, "evidence", _strings(self.evidence, label="确认证据"))
        if not self.evidence:
            raise ValueError("confirmation 缺少 evidence")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable frontmatter wire format."""

        return {
            "mode": self.mode,
            "confirmedBy": self.confirmed_by,
            "confirmedAt": self.confirmed_at,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfirmationProvenance:
        """Construct from validated or caller-provided frontmatter data."""

        if not isinstance(data, dict):
            raise ValueError("confirmation 必须为映射")
        return cls(
            mode=data.get("mode", ""),
            confirmed_by=data.get("confirmedBy", ""),
            confirmed_at=data.get("confirmedAt", ""),
            evidence=tuple(data.get("evidence") or ()),
        )


# 源码位置独立于分析器状态，可作为人工确认的稳定锚点。
# 行号显式排除布尔值，并要求结束行不早于起始行。
@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A verified source symbol and its location, independent of analysis availability."""

    path: str
    symbol: str
    start_line: int
    end_line: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not self.path
            or self.path.startswith("/")
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or any(ord(char) < 32 for char in self.path)
        ):
            raise ValueError("源码位置 path 必须为仓库相对路径")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("源码位置需要完整限定符号")
        # bool 是 int 的子类，但协议行号必须是真正整数。
        if type(self.start_line) is not int or self.start_line < 1:
            raise ValueError("源码位置 startLine 必须为正整数")
        if self.end_line is not None and (
            type(self.end_line) is not int or self.end_line < self.start_line
        ):
            raise ValueError("源码位置 endLine 不能小于 startLine")

    def to_dict(self) -> dict[str, Any]:
        """Serialize using source-anchor field names."""

        result: dict[str, Any] = {
            "path": self.path,
            "symbol": self.symbol,
            "startLine": self.start_line,
        }
        if self.end_line is not None:
            result["endLine"] = self.end_line
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceLocation:
        """Construct a validated location from source-anchor data."""

        if not isinstance(data, dict) or not {"path", "symbol", "startLine"} <= data.keys():
            raise ValueError("源码位置需要 path、symbol 与 startLine")
        return cls(data["path"], data["symbol"], data["startLine"], data.get("endLine"))


# 将身份、状态和锚点封装为不可变记录，构造时完成跨字段校验。
# 源码位置补入符号集合，使两种锚点表示保持一致。
# 序列化省略未填写的可选字段，减少无意义的正文变更。
@dataclass(frozen=True, slots=True)
class ModuleMeta:
    """Validated Module identity metadata; new records start as ``observed``."""

    module_id: ModuleId
    parent: str = ""
    state: str = "observed"
    realization: str = "implemented"
    aliases: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    responsibilities: str = ""
    anchor_paths: tuple[str, ...] = ()
    anchor_symbols: tuple[str, ...] = ()
    anchor_apis: tuple[str, ...] = ()
    anchor_routes: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()
    contract_ids: tuple[str, ...] = ()
    security_sensitive: bool = False
    confirmation: ConfirmationProvenance | None = None
    anchor_locations: tuple[SourceLocation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.module_id, ModuleId):
            raise ValueError("module_id 必须为 ModuleId")
        parent = self.parent or self.module_id.domain
        parent = _segment(parent, label="parent")
        # 逻辑父领域必须与物理身份一致，模块不能通过元数据跨领域重归属。
        if parent != self.module_id.domain:
            raise ValueError("parent 必须等于 module_id 的领域片段")
        object.__setattr__(self, "parent", parent)
        if any(not isinstance(item, SourceLocation) for item in self.anchor_locations):
            raise ValueError("anchor_locations 必须包含 SourceLocation 值")
        object.__setattr__(
            self,
            "anchor_locations",
            tuple(
                sorted(
                    set(self.anchor_locations),
                    key=lambda x: (x.path, x.symbol, x.start_line, x.end_line or x.start_line),
                )
            ),
        )
        if self.state not in MODULE_STATES:
            raise ValueError(f"state 可选值为： {', '.join(sorted(MODULE_STATES))}")
        if self.realization not in SCOPE_REALIZATIONS:
            raise ValueError("realization 必须为 business-only 或 implemented")
        object.__setattr__(self, "aliases", _deterministic_strings(self.aliases, label="aliases"))
        object.__setattr__(self, "owners", _deterministic_strings(self.owners, label="owners"))
        object.__setattr__(
            self,
            "anchor_paths",
            _deterministic_strings(self.anchor_paths, label="anchor_paths"),
        )
        object.__setattr__(
            self,
            "anchor_symbols",
            tuple(
                sorted(
                    set(_deterministic_strings(self.anchor_symbols, label="anchor_symbols"))
                    | {item.symbol for item in self.anchor_locations}
                )
            ),
        )
        object.__setattr__(
            self,
            "anchor_apis",
            _deterministic_strings(self.anchor_apis, label="anchor_apis"),
        )
        object.__setattr__(
            self,
            "anchor_routes",
            _deterministic_strings(self.anchor_routes, label="anchor_routes"),
        )
        object.__setattr__(
            self, "surfaces", _deterministic_strings(self.surfaces, label="surfaces")
        )
        object.__setattr__(
            self,
            "contract_ids",
            tuple(
                sorted(
                    _contract_id(value, label="contract_ids")
                    for value in _deterministic_strings(self.contract_ids, label="contract_ids")
                )
            ),
        )
        if not isinstance(self.responsibilities, str):
            raise ValueError("responsibilities 必须为字符串")
        if not isinstance(self.security_sensitive, bool):
            raise ValueError("security_sensitive 必须为布尔值")
        if self.confirmation is not None and not isinstance(
            self.confirmation, ConfirmationProvenance
        ):
            raise ValueError("confirmation 必须为 ConfirmationProvenance")
        # 最终序列化形状整体检查，覆盖嵌套锚点与确认来源中的敏感文本。
        if contains_sensitive_literal(self.to_dict()):
            raise ValueError("ModuleMeta 不能包含敏感字面量")

    # 读取文档后核对磁盘身份，避免文件移动留下错误的模块归属。
    def assert_physical_identity(self, domain: str, module: str) -> None:
        physical = ModuleId(domain, module)
        if physical != self.module_id:
            raise ValueError(f"ModuleMeta 身份 {self.module_id} 与实际路径不匹配：{physical}")

    # 只有已确认且有实现的模块进入实现视图，候选观察不具备同等权威。
    @property
    def implemented(self) -> bool:
        return self.state == "confirmed" and self.realization == "implemented"

    # 空锚点集合不写入协议，保持缺省表示简洁且稳定。
    def _anchors_dict(self) -> dict[str, Any]:
        """Serialize only populated source-anchor collections."""

        anchors: dict[str, Any] = {}
        for key, values in (
            ("paths", self.anchor_paths),
            ("symbols", self.anchor_symbols),
            ("apis", self.anchor_apis),
            ("routes", self.anchor_routes),
        ):
            if values:
                anchors[key] = list(values)
        if self.anchor_locations:
            anchors["locations"] = [item.to_dict() for item in self.anchor_locations]
        return anchors

    def to_dict(self) -> dict[str, Any]:
        """Serialize deterministic, omission-aware Module frontmatter."""

        data: dict[str, Any] = {
            "schemaVersion": MODULE_META_SCHEMA_VERSION,
            "moduleId": str(self.module_id),
            "parent": self.parent,
            "state": self.state,
            "realization": self.realization,
            "surfaces": list(self.surfaces),
            "contractIds": list(self.contract_ids),
        }
        if self.aliases:
            data["aliases"] = list(self.aliases)
        if self.owners:
            data["owners"] = list(self.owners)
        if self.responsibilities:
            data["responsibilities"] = self.responsibilities
        anchors = self._anchors_dict()
        if anchors:
            data["anchors"] = anchors
        if self.security_sensitive:
            data["securitySensitive"] = True
        if self.confirmation is not None:
            data["confirmation"] = self.confirmation.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModuleMeta:
        """Validate and construct Module metadata from frontmatter."""

        errors = validate_module_meta(data)
        if errors:
            raise ValueError("ModuleMeta 无效： " + "; ".join(errors))
        module_id = ModuleId.parse(data["moduleId"])
        anchors = data.get("anchors") or {}
        confirmation = data.get("confirmation")
        return cls(
            module_id=module_id,
            parent=data.get("parent", module_id.domain),
            state=data.get("state", "observed"),
            realization=data.get("realization", "implemented"),
            aliases=tuple(data.get("aliases") or ()),
            owners=tuple(data.get("owners") or ()),
            responsibilities=data.get("responsibilities", ""),
            anchor_paths=tuple(anchors.get("paths") or ()),
            anchor_symbols=tuple(anchors.get("symbols") or ()),
            anchor_apis=tuple(anchors.get("apis") or ()),
            anchor_routes=tuple(anchors.get("routes") or ()),
            anchor_locations=tuple(
                SourceLocation.from_dict(item) for item in anchors.get("locations", ())
            ),
            surfaces=tuple(data.get("surfaces") or ()),
            contract_ids=tuple(data.get("contractIds") or ()),
            security_sensitive=bool(data.get("securitySensitive")),
            confirmation=(ConfirmationProvenance.from_dict(confirmation) if confirmation else None),
        )


# 统一返回全部契约错误，让编辑者一次修复完整输入。
def validate_module_meta(data: dict[str, Any]) -> list[str]:
    """Return all schema violations in deterministic JSONPath order."""

    return _schema_errors(MODULE_META_SCHEMA, data)


__all__ = [
    "MODULE_META_SCHEMA",
    "MODULE_META_SCHEMA_VERSION",
    "MODULE_STATES",
    "SCOPE_REALIZATIONS",
    "ConfirmationProvenance",
    "ModuleId",
    "ModuleMeta",
    "ModuleState",
    "ScopeKind",
    "ScopeRealization",
    "ScopeRef",
    "SourceLocation",
    "module_meta_field_names",
    "validate_module_meta",
]
