"""Typed project-level Modus configuration."""

from __future__ import annotations

import re
import warnings
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, cast

import yaml

from .artifacts_io import CorruptArtifactError, atomic_write_text, safe_load_yaml
from .paths import ModusPaths

ConfigDisposition: TypeAlias = Literal["absent", "workspace", "preserve", "retire"]

VALID_PROJECT_KINDS: Final = frozenset({"project", "workspace"})
VALID_ENTRY_KINDS: Final = frozenset(
    {"external-http", "external-rpc", "cli", "callback", "internal", "job", "mq"}
)
VALID_EFFECT_KINDS: Final = frozenset(
    {
        "dependency",
        "entity",
        "table",
        "state",
        "authoritative-write",
        "transaction",
        "lock",
        "idempotency",
        "rule",
    }
)
VALID_RESOURCE_KINDS: Final = frozenset(
    {"config", "cache", "queue", "topic", "table", "object", "external-system"}
)
_PROJECT_CONFIG_METADATA_FIELDS = frozenset({"kind", "workspace"})


# 每次构造返回独立字典，某个项目调整预算不会修改全局默认值。
# 冷启动时间、候选数与输出长度分别约束发现成本和消费成本。
def _default_heuristic_grounding() -> dict[str, Any]:
    return {
        "enabled": True,
        "cold_budget_ms": 3000,
        "max_context_chars": 12000,
        "max_domains": 3,
        "max_members": 3,
        "max_candidates": 24,
        "max_body_sections": 6,
    }


# 默认项目没有成员，成员引用必须由工作区配置显式声明。
def _default_workspace() -> dict[str, Any]:
    return {"members": []}


# 嵌套配置采用封闭键集合，拼写错误不能静默退回默认行为。
# 错误路径由调用方传入，使深层校验仍能指出具体配置位置。
def _closed(value: object, *, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须为映射")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{name} 包含未知键： {', '.join(unknown)}")
    return value


# 配置边界拒绝重复项，而非静默去重，避免隐藏用户错误意图。
def _strings(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{name} 必须为非空字符串列表")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} 不能包含重复项")
    return tuple(value)


# 构建模型查询与注解处理分别授权，发现源码不自动启用执行扩展。
# 显式描述文件可替代工具查询，具体路径约束由构建解析器执行。
@dataclass(frozen=True, slots=True)
class JavaAnalysisSettings:
    build_system: str = "auto"
    classpath_file: str = ""
    build_model_file: str = ""
    allow_gradle_model_query: bool = False
    annotation_processing: bool = False


# SCIP 索引是可选增强输入，缺省仍允许内置观察路径。
@dataclass(frozen=True, slots=True)
class PythonAnalysisSettings:
    provider: str = "auto"
    scip_index: str = ""


# 包选择、构建标签与 cgo 分别定义构建上下文。
# 元组保持稳定顺序，并避免多个配置实例共享可变列表。
@dataclass(frozen=True, slots=True)
class GoAnalysisSettings:
    patterns: tuple[str, ...] = ("./...",)
    build_tags: tuple[str, ...] = ()
    cgo: str = "off"


# 编译数据库描述每个翻译单元，依赖扫描决定是否纳入项目头文件闭包。
@dataclass(frozen=True, slots=True)
class CppAnalysisSettings:
    compilation_database: str = ""
    dependency_scan: bool = True


# 模式只规定提供器是否必需，各语言仍拥有独立的配置边界。
# 工厂默认值避免嵌套设置在项目之间共享实例。
@dataclass(frozen=True, slots=True)
class CompilerSettings:
    mode: str = "auto"
    java: JavaAnalysisSettings = field(default_factory=JavaAnalysisSettings)
    python: PythonAnalysisSettings = field(default_factory=PythonAnalysisSettings)
    go: GoAnalysisSettings = field(default_factory=GoAnalysisSettings)
    cpp: CppAnalysisSettings = field(default_factory=CppAnalysisSettings)


# 仓库标识用于跨仓库拓扑引用，不应从本机绝对路径推导。
@dataclass(frozen=True, slots=True)
class TopologySettings:
    repository_id: str = ""


# 后缀先限定候选文件，正则再观察源码片段；两者不执行业务代码。
@dataclass(frozen=True, slots=True)
class RuleMatch:
    regex: str
    suffixes: tuple[str, ...] = ()


# type 决定证据类别，kind 在所属词汇中解释，不能跨类别混用。
# contract 与 observation 是声明文本，不能据此升级为已证明的运行行为。
@dataclass(frozen=True, slots=True)
class RuleEmit:
    type: Literal["entry", "effect", "resource"]
    kind: str
    contract: str = ""
    observation: str = ""


# 规则 ID 是稳定来源身份，匹配与输出策略独立便于审查。
@dataclass(frozen=True, slots=True)
class AuthoringRule:
    id: str
    match: RuleMatch
    emit: RuleEmit


# 外部规则包与内联规则共享下游类型契约，装载来源仍可分别追踪。
@dataclass(frozen=True, slots=True)
class AuthoringRulesSettings:
    packs: tuple[str, ...] = ()
    rules: tuple[AuthoringRule, ...] = ()


# 初始化策略集中组合，普通只读检索无需因此加载分析器实现。
@dataclass(frozen=True, slots=True)
class InitAnalysisSettings:
    compiler: CompilerSettings = field(default_factory=CompilerSettings)
    topology: TopologySettings = field(default_factory=TopologySettings)
    authoring_rules: AuthoringRulesSettings = field(default_factory=AuthoringRulesSettings)


# 允许字段直接来自 dataclass 声明，新增字段不需维护第二份键名单。
# Go 的 YAML 列表在边界转成元组，再交给不可变语言设置。
def _language(value: object, cls: type[Any], *, name: str) -> Any:
    allowed = {item.name for item in fields(cls)}
    row = _closed(value or {}, allowed=allowed, name=name)
    if cls is GoAnalysisSettings:
        row = {
            **row,
            "patterns": _strings(row.get("patterns", ["./..."]), name=f"{name}.patterns"),
            "build_tags": _strings(row.get("build_tags", []), name=f"{name}.build_tags"),
        }
    return cls(**row)


# 规则先完成语法与输出词汇验证，再进入观察流水线。
# 正则在配置读取时编译校验，避免扫描到某个文件才发现表达式无效。
# 规则 ID 不允许空白，保证来源标识可以稳定序列化和引用。
# 后缀要求点前缀，减少文件类型匹配中的歧义。
def _rule(value: object, *, index: int) -> AuthoringRule:
    prefix = f"init_analysis.authoring_rules.rules[{index}]"
    row = _closed(value, allowed={"id", "match", "emit"}, name=prefix)
    rule_id = row.get("id")
    if (
        not isinstance(rule_id, str)
        or not rule_id
        or rule_id != rule_id.strip()
        or any(character.isspace() for character in rule_id)
    ):
        raise ValueError(f"{prefix}.id 必须为不含空白的单个标识")
    match = _closed(row.get("match"), allowed={"regex", "suffixes"}, name=f"{prefix}.match")
    pattern = match.get("regex")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{prefix}.match.regex 必须为非空字符串")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{prefix}.match.regex 无效： {exc}") from exc
    suffixes = _strings(match.get("suffixes", []), name=f"{prefix}.match.suffixes")
    if any(not suffix.startswith(".") for suffix in suffixes):
        raise ValueError(f"{prefix}.match.suffixes 必须以点开头")
    emit = _closed(
        row.get("emit"),
        allowed={"type", "kind", "contract", "observation"},
        name=f"{prefix}.emit",
    )
    emit_type = emit.get("type")
    kind = emit.get("kind")
    allowed_kinds = {
        "entry": VALID_ENTRY_KINDS,
        "effect": VALID_EFFECT_KINDS,
        "resource": VALID_RESOURCE_KINDS,
    }
    if emit_type not in allowed_kinds or kind not in allowed_kinds[emit_type]:
        raise ValueError(f"{prefix}.emit 必须声明受支持的 type 与 kind")
    contract = emit.get("contract", "")
    observation = emit.get("observation", "")
    if not isinstance(contract, str) or not isinstance(observation, str):
        raise ValueError(f"{prefix}.emit 的 contract 与 observation 必须为字符串")
    return AuthoringRule(
        rule_id,
        RuleMatch(pattern, suffixes),
        RuleEmit(cast(Any, emit_type), cast(str, kind), contract, observation),
    )


# 规则包和内联配置共用同一校验器，不能因装载位置不同放宽形状。
# ID 唯一性在整组解析后确认，避免后规则悄悄覆盖前规则。
def parse_authoring_rules(values: object, *, source: str = "rules") -> tuple[AuthoringRule, ...]:
    """Validate inline and packed authoring rules through one typed contract."""

    if not isinstance(values, list):
        raise ValueError(f"{source} 必须为列表")
    result = tuple(_rule(item, index=index) for index, item in enumerate(values))
    if len({item.id for item in result}) != len(result):
        raise ValueError(f"{source} 规则 ID 必须唯一")
    return result


# 已经规范化的设置可直接复用，原始映射则逐层收口验证。
# 枚举值在配置边界拒绝，提供器不必各自猜测未知模式。
# 仓库 ID 排除空白和控制字符，避免污染后续稳定标识。
# 独立组装编译、拓扑和创作规则，保留各领域的清晰所有权。
def _init_analysis(value: object) -> InitAnalysisSettings:
    if isinstance(value, InitAnalysisSettings):
        return value
    row = _closed(
        value or {},
        allowed={"compiler", "topology", "authoring_rules"},
        name="init_analysis",
    )
    compiler_row = _closed(
        row.get("compiler") or {},
        allowed={"mode", "java", "python", "go", "cpp"},
        name="init_analysis.compiler",
    )
    mode = compiler_row.get("mode", "auto")
    if mode not in {"auto", "required", "off"}:
        raise ValueError("init_analysis.compiler.mode 必须为 auto、required 或 off")
    compiler = CompilerSettings(
        mode=cast(str, mode),
        java=_language(
            compiler_row.get("java"), JavaAnalysisSettings, name="init_analysis.compiler.java"
        ),
        python=_language(
            compiler_row.get("python"),
            PythonAnalysisSettings,
            name="init_analysis.compiler.python",
        ),
        go=_language(compiler_row.get("go"), GoAnalysisSettings, name="init_analysis.compiler.go"),
        cpp=_language(
            compiler_row.get("cpp"), CppAnalysisSettings, name="init_analysis.compiler.cpp"
        ),
    )
    if compiler.java.build_system not in {"auto", "maven", "gradle", "none"}:
        raise ValueError("init_analysis.compiler.java.build_system 无效")
    if compiler.python.provider not in {"auto", "builtin", "scip"}:
        raise ValueError("init_analysis.compiler.python.provider 无效")
    if compiler.go.cgo not in {"off", "auto"}:
        raise ValueError("init_analysis.compiler.go.cgo 无效")
    topology_row = _closed(
        row.get("topology") or {}, allowed={"repository_id"}, name="init_analysis.topology"
    )
    repository_id = topology_row.get("repository_id", "")
    if not isinstance(repository_id, str) or any(
        character.isspace() or ord(character) < 32 for character in repository_id
    ):
        raise ValueError("init_analysis.topology.repository_id 无效")
    rules_row = _closed(
        row.get("authoring_rules") or {},
        allowed={"packs", "rules"},
        name="init_analysis.authoring_rules",
    )
    packs = _strings(rules_row.get("packs", []), name="init_analysis.authoring_rules.packs")
    raw_rules = rules_row.get("rules", [])
    rules = parse_authoring_rules(raw_rules, source="init_analysis.authoring_rules.rules")
    return InitAnalysisSettings(
        compiler,
        TopologySettings(repository_id),
        AuthoringRulesSettings(packs, rules),
    )


# id、repositoryId 与路径分别校验，显示名不能替代可寻址身份。
# 成员路径必须是规范相对路径，不接受点段、重复分隔符或反斜线。
# 这里验证配置形状，成员是否存在及是否 Git 根由工作区发现负责。
def _normalize_workspace_member(member: object, *, index: int) -> dict[str, str]:
    prefix = f"workspace.members[{index}]"
    row = _closed(member, allowed={"id", "path", "repositoryId"}, name=prefix)
    member_id = row.get("id")
    path = row.get("path")
    repository_id = row.get("repositoryId", member_id)
    for name, item in (("id", member_id), ("repositoryId", repository_id)):
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or item in {".", ".."}
            or "/" in item
            or "\\" in item
            or (name == "repositoryId" and ":" in item)
            or any(character.isspace() or ord(character) < 32 for character in item)
        ):
            raise ValueError(f"{prefix}.{name} 必须为单个规范片段")
    if (
        not isinstance(path, str)
        or not path
        or path != path.strip()
        or path.startswith(("/", "\\"))
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError(f"{prefix}.path 必须为规范的相对路径")
    return {
        "id": cast(str, member_id),
        "path": path,
        "repositoryId": cast(str, repository_id),
    }


# 序列化回普通容器，使 YAML 不携带 Python 专用 tuple 标签。
def _plain(value: object) -> Any:
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


# 未知键仅作为读取诊断，故意不成为可写回的 dataclass 字段。
class _ConfigObservations:
    unknown_keys: list[str]


# 聚合对象在构造完成时即满足类型与项目种类约束。
# 运行设置可调，初始化阶段的设置则以不可变值对象传递。
@dataclass
class ModusConfig(_ConfigObservations):
    """Validated aggregate for project, workspace and initialization policy."""

    ripgrep_fallback: bool = True
    heuristic_grounding: dict[str, Any] = field(default_factory=_default_heuristic_grounding)
    init_analysis: InitAnalysisSettings = field(default_factory=InitAnalysisSettings)
    kind: str = "project"
    workspace: dict[str, Any] = field(default_factory=_default_workspace)

    def __post_init__(self) -> None:
        if self.kind not in VALID_PROJECT_KINDS:
            raise ValueError(f"kind 可选值为： {', '.join(sorted(VALID_PROJECT_KINDS))}")
        self._validate_runtime_settings()
        self.init_analysis = _init_analysis(self.init_analysis)
        self._validate_workspace()

    # 只补齐缺省键，不接管未知配置；错误不能被默认值吞掉。
    @staticmethod
    def _merged_settings(
        value: dict[str, Any], defaults: dict[str, Any], name: str
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{name} 必须为映射，实际为 {type(value).__name__}")
        unknown = sorted(set(value) - set(defaults))
        if unknown:
            raise ValueError(f"{name} 包含未知键： {', '.join(unknown)}")
        return {**defaults, **value}

    # 布尔值在 Python 中也是整数，预算必须显式排除 bool。
    # 正整数下限保证后续截断与计时逻辑存在可用的最小工作量。
    def _validate_runtime_settings(self) -> None:
        value = self._merged_settings(
            self.heuristic_grounding,
            _default_heuristic_grounding(),
            "heuristic_grounding",
        )
        if not isinstance(value["enabled"], bool):
            raise ValueError("heuristic_grounding.enabled 必须为布尔值")
        for key in (
            "cold_budget_ms",
            "max_context_chars",
            "max_domains",
            "max_members",
            "max_candidates",
            "max_body_sections",
        ):
            item = value[key]
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError(f"heuristic_grounding.{key} 必须为正整数")
        self.heuristic_grounding = value

    # 成员 ID、路径和仓库 ID 都需唯一，三者冲突会产生不同的寻址歧义。
    # 普通项目不能夹带成员引用，工作区则至少需要两个已确认成员。
    def _validate_workspace(self) -> None:
        value = self._merged_settings(self.workspace, _default_workspace(), "workspace")
        members = value["members"]
        if not isinstance(members, list):
            raise ValueError("workspace.members 必须为列表")
        normalized = [
            _normalize_workspace_member(member, index=index) for index, member in enumerate(members)
        ]
        identities = [item["id"] for item in normalized]
        paths = [item["path"] for item in normalized]
        repositories = [item["repositoryId"] for item in normalized]
        if any(len(values) != len(set(values)) for values in (identities, paths, repositories)):
            raise ValueError("工作区成员的 id、path 和 repositoryId 必须唯一")
        if self.kind == "workspace" and len(normalized) < 2:
            raise ValueError("kind: workspace 需要至少两个已确认的 workspace.members")
        if self.kind == "project" and normalized:
            raise ValueError("kind: project 不能配置 workspace.members")
        value["members"] = normalized
        self.workspace = value

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _plain(asdict(self)))

    # 顶层未知键为兼容旧配置保留诊断；嵌套策略仍执行封闭校验。
    # 记录 unknown_keys 可阻止清理流程误删含未理解设置的文件。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModusConfig:
        if not isinstance(data, dict):
            raise ValueError(
                f".modus/config.yaml 必须为 key: value 映射，实际为 {type(data).__name__}"
            )
        known = {item.name for item in fields(cls)}
        unknown = sorted(key for key in data if key not in known)
        if unknown:
            warnings.warn(
                f"忽略 .modus/config.yaml 中的未知配置键： {', '.join(unknown)}",
                stacklevel=2,
            )
        obj = cls(**{key: value for key, value in data.items() if key in known})
        obj.unknown_keys = unknown
        return obj

    # 用原子替换发布完整 YAML，读取者不应看到半写入的配置。
    def write(self, paths: ModusPaths) -> Path:
        paths.config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            paths.config_path,
            yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return paths.config_path

    # 尽力读取用于可降级观察场景；需要严格前置条件的写入方应调用 read。
    @classmethod
    def read_available(cls, paths: ModusPaths) -> ModusConfig:
        try:
            return cls.read(paths)
        except (CorruptArtifactError, OSError, ValueError, TypeError):
            return cls()

    # 缺少可选配置使用默认值，损坏配置则保留明确异常与恢复提示。
    @classmethod
    def read(cls, paths: ModusPaths) -> ModusConfig:
        if not paths.config_path.is_file():
            return cls()
        data = safe_load_yaml(
            paths.config_path,
            kind="项目配置",
            default={},
            hint="请修复 .modus/config.yaml，或重新运行 modus init 恢复有效配置。",
        )
        return cls.from_dict(data)


# 只有完全默认且没有未知键的项目配置可退役。
# 工作区成员表和任何用户调优都必须保留，不能仅按文件名判断可删除。
# 软链或非普通文件无法证明所有权，直接拒绝清理。
def project_config_disposition(paths: ModusPaths) -> ConfigDisposition:
    target = paths.config_path
    if not target.exists() and not target.is_symlink():
        return "absent"
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"项目配置 不是常规文件： {target}")
    current = ModusConfig.read(paths)
    if current.kind == "workspace":
        return "workspace"
    if getattr(current, "unknown_keys", None):
        return "preserve"
    defaults = ModusConfig()
    for config_field in fields(current):
        name = config_field.name
        if name not in _PROJECT_CONFIG_METADATA_FIELDS and getattr(current, name) != getattr(
            defaults, name
        ):
            return "preserve"
    return "retire"


# 明确工作区配置才写入；普通项目只按既有配置处置结果清理。
# 返回处置词汇供报告使用，不将保留操作误报成写入。
def reconcile_optional_config(
    paths: ModusPaths,
    *,
    workspace_config: ModusConfig | None = None,
) -> str:
    if workspace_config is not None:
        if workspace_config.kind != "workspace":
            raise ValueError("workspace_config 必须设置 kind='workspace'")
        workspace_config.write(paths)
        return "workspace-written"
    disposition = project_config_disposition(paths)
    if disposition == "retire":
        paths.config_path.unlink()
        return "retired"
    return disposition


__all__ = [
    "AuthoringRule",
    "AuthoringRulesSettings",
    "CompilerSettings",
    "ConfigDisposition",
    "InitAnalysisSettings",
    "ModusConfig",
    "TopologySettings",
    "VALID_EFFECT_KINDS",
    "VALID_ENTRY_KINDS",
    "VALID_PROJECT_KINDS",
    "VALID_RESOURCE_KINDS",
    "project_config_disposition",
    "parse_authoring_rules",
    "reconcile_optional_config",
]
