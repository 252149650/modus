"""Pure classification and ownership rules for topology projection.

Compiler-exact binding establishes which symbol was called, not which business
or infrastructure role it plays. Resolution caps preserve that distinction as
annotation-backed and keyword-based observations enter the same topology.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from modus.machine.facts.semantic_ir import SemanticEdge, SemanticFacts, SemanticNode
from modus.machine.facts.value_summary import ValueSummary
from modus.machine.resolution.topology_model import (
    resolution_rank as _resolution_rank,
)
from modus.machine.resolution.topology_model import (
    semantic_owner as _owner,
)

_COMPONENT_ANNOTATIONS = (
    "Service",
    "Component",
    "Controller",
    "RestController",
    "Repository",
    "Configuration",
    "DubboService",
)
_ENDPOINT_ANNOTATIONS = (
    "RequestMapping",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
    "GrpcService",
)
_READ_OPERATIONS = re.compile(r"^(get|find|list|query|select|count|load|read|scan)", re.I)
_WRITE_OPERATIONS = re.compile(
    r"^(set|put|add|create|insert|update|save|delete|remove|write)", re.I
)

# A resource/RPC edge's `resolution` must never claim more certainty than how
# the edge was classified. Compiler-exact call resolution says nothing about
# whether a keyword match correctly guessed the target's infrastructure role.
_CLASSIFICATION_RESOLUTION_CAP: dict[str, str | None] = {
    "annotation-verified": None,
    "rule-pack-match": None,
    "keyword-heuristic": "heuristic",
    "unclassified": "unresolved",
}


# 聚合选择最佳见证等级，空集合保持未解析。
def _strongest_resolution(values: Iterable[str]) -> str:
    return max(values, key=_resolution_rank, default="unresolved")


# 调用绑定与资源分类是两种证据，最终等级不能超出分类依据。
def _capped_resolution(call_resolution: str, tier: str) -> str:
    cap = _CLASSIFICATION_RESOLUTION_CAP.get(tier)
    if cap is None or _resolution_rank(call_resolution) <= _resolution_rank(cap):
        return call_resolution
    return cap


# 失效操作先于一般写操作判断，保留缓存删除的独立语义。
def _cache_relation(operation: str) -> str:
    if _READ_OPERATIONS.search(operation):
        return "reads"
    if operation.lower().startswith(("delete", "remove", "evict", "invalidate", "clear")):
        return "invalidates"
    return "writes" if _WRITE_OPERATIONS.search(operation) else "uses"


# 生产与消费根据显式操作词区分，未知操作保持一般使用关系。
def _mq_relation(operation: str) -> str:
    lowered = operation.lower()
    if any(marker in lowered for marker in ("consume", "listen", "receive", "poll")):
        return "consumes"
    return "publishes" if lowered.startswith(("send", "publish", "produce", "emit")) else "uses"


# 无法识别的数据库操作不猜测读写性质，回退到 uses。
def _db_relation(operation: str) -> str:
    if _READ_OPERATIONS.search(operation):
        return "reads"
    if _WRITE_OPERATIONS.search(operation):
        return "writes"
    return "uses"


def _config_relation(_operation: str) -> str:
    return "configured_by"


@dataclass(frozen=True)
class _ResourceCategory:
    """One declarative, extensible built-in resource-classification rule.

    Each category projects the same
    ``(kind, relation, resource_name, framework, tier, reasonCode)`` shape.
    New framework markers belong here; resource lifting consumes the result
    without embedding classification policy in its call-site loop.
    """

    category_id: str
    resource_kind: str
    default_resource_name: str
    relation_of: Callable[[str], str]
    frameworks: tuple[tuple[str, str], ...]  # (marker substring, framework label), ordered
    annotation_markers: tuple[str, ...] = ()  # method/type annotations that upgrade the tier

    # 规则顺序即匹配优先级，新增宽泛标记时应置于更具体标记之后。
    def matched_framework(self, lowered_target: str) -> str | None:
        for marker, framework in self.frameworks:
            if marker.startswith(r"\b"):
                if re.search(marker, lowered_target):
                    return framework
            elif marker in lowered_target:
                return framework
        return None


_BUILTIN_RESOURCE_CATEGORIES: tuple[_ResourceCategory, ...] = (
    _ResourceCategory(
        category_id="cache",
        resource_kind="keyspace",
        default_resource_name="default",
        relation_of=_cache_relation,
        frameworks=(
            ("redisson", "redis"),
            ("jedis", "redis"),
            ("lettuce", "redis"),
            ("redis", "redis"),
            ("ckv", "ckv"),
            ("caffeine", "caffeine"),
            ("loadingcache", "caffeine"),
            ("cachebuilder", "guava-cache"),
        ),
        annotation_markers=("Cacheable", "CachePut", "CacheEvict", "CacheConfig"),
    ),
    _ResourceCategory(
        category_id="mq",
        resource_kind="topic",
        default_resource_name="dynamic-topic",
        relation_of=_mq_relation,
        frameworks=(
            ("kafka", "kafka"),
            ("pulsar", "pulsar"),
            ("rocketmq", "rocketmq"),
            ("rabbitmq", "rabbitmq"),
            ("amqptemplate", "rabbitmq"),
            ("rabbittemplate", "rabbitmq"),
            ("messageproducer", "mq"),
            ("messageconsumer", "mq"),
        ),
        annotation_markers=(
            "KafkaListener",
            "RabbitListener",
            "JmsListener",
            "RocketMQMessageListener",
        ),
    ),
    _ResourceCategory(
        category_id="database",
        resource_kind="db",
        default_resource_name="dynamic-database",
        relation_of=_db_relation,
        frameworks=(
            ("mybatis", "mybatis"),
            ("mapper", "mybatis"),
            ("jdbc", "jdbc"),
            ("jpa", "jpa"),
            ("entitymanager", "jpa"),
            ("mongotemplate", "mongodb"),
            ("mongorepository", "mongodb"),
            ("mongocollection", "mongodb"),
            ("mongodb", "mongodb"),
            ("elasticsearchresttemplate", "elasticsearch"),
            ("resthighlevelclient", "elasticsearch"),
            ("elasticsearch", "elasticsearch"),
            ("sql", "database"),
        ),
    ),
    _ResourceCategory(
        category_id="config",
        resource_kind="config-key",
        default_resource_name="dynamic-key",
        relation_of=_config_relation,
        frameworks=(
            ("apollo", "apollo"),
            ("nacos", "nacos"),
            (r"\bconfig\b", "configuration"),
            ("viper", "viper"),
            ("getenv", "environment"),
        ),
    ),
)


# 容忍缺失注解字段，但不把任意标量拆解为注解列表。
def _annotations(node: SemanticNode) -> tuple[str, ...]:
    raw = node.attributes.get("annotations", ())
    return tuple(str(item) for item in raw) if isinstance(raw, (list, tuple)) else ()


def _annotation_type(annotation: str) -> str:
    match = re.match(r"\s*@?([A-Za-z_$][\w.$]*)", annotation)
    return match.group(1) if match else ""


# 限定名需来自已知框架命名空间，减少业务同名注解误分类。
def _has_annotation(annotations: Iterable[str], names: Iterable[str]) -> bool:
    expected = frozenset(names)
    for item in annotations:
        qualified = _annotation_type(item)
        if qualified.rsplit(".", 1)[-1] not in expected:
            continue
        if "." not in qualified or qualified.startswith(
            (
                "org.springframework.",
                "org.apache.dubbo.",
                "com.alibaba.dubbo.",
                "net.devh.boot.grpc.",
                "io.grpc.",
                "org.apache.kafka.",
            )
        ):
            return True
    return False


# 简单名可能存在歧义，只有认可的限定名可提供精确注解证据。
def _annotation_resolution(annotations: Iterable[str]) -> str:
    names = (
        *_COMPONENT_ANNOTATIONS,
        *_ENDPOINT_ANNOTATIONS,
        "SpringBootApplication",
        "Cacheable",
        "CachePut",
        "CacheEvict",
        "KafkaListener",
        "RabbitListener",
    )
    return (
        "exact"
        if any(
            "." in _annotation_type(item) and _has_annotation((item,), names)
            for item in annotations
        )
        else "candidate"
    )


def _has_composed_annotation(annotations: Iterable[str], names: Iterable[str]) -> bool:
    # A suffix is not a meta-annotation relationship. Custom composed
    # annotations require an observed annotation-declaration relation.
    return _has_annotation(annotations, names)


# 区分 Dubbo Service 与普通 Service，避免把本地组件当作 RPC 服务。
def _is_dubbo_service(annotations: Iterable[str]) -> bool:
    for item in annotations:
        qualified = _annotation_type(item)
        simple = qualified.rsplit(".", 1)[-1]
        if simple == "DubboService" and _has_annotation((item,), ("DubboService",)):
            return True
        if simple == "Service" and (
            qualified.startswith("org.apache.dubbo.") or qualified.startswith("com.alibaba.dubbo.")
        ):
            return True
    return False


def _is_grpc_service(annotations: Iterable[str]) -> bool:
    return _has_annotation(annotations, ("GrpcService",))


# 仅提取指定参数的字符串字面量，不执行配置或表达式求值。
def _annotation_value(annotations: str, name: str) -> str:
    annotations = annotations.replace('\\"', '"').replace("\\'", "'")
    match = re.search(rf"\b{re.escape(name)}\s*=\s*[\"']([^\"']*)[\"']", annotations)
    return match.group(1) if match else ""


# 接口所有者、分组和版本共同定义 RPC 契约，防止同名不同版本混用。
def _dubbo_contract(qualified_method: str, annotations: str) -> str:
    owner, _, descriptor = qualified_method.partition("#")
    group = _annotation_value(annotations, "group")
    version = _annotation_value(annotations, "version")
    return f"dubbo:{owner}:{group}:{version}#{descriptor}"


def _grpc_contract(qualified_method: str) -> str:
    owner, separator, descriptor = qualified_method.partition("#")
    method = descriptor.partition("(")[0] if separator else qualified_method.partition("(")[0]
    return f"grpc:{owner}#{method}"


# 消除重复斜杠和路径参数正则，仅保留用于契约比较的路由形状。
def _normalized_route(value: str) -> str:
    route = value.strip()
    if not route.startswith("/"):
        route = "/" + route
    route = re.sub(r"/+", "/", route)
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", route)


# 空路径与动态路径分别返回空串和未知，避免把动态前缀静默删除。
def _mapping_route(annotations: str) -> str | None:
    """Read only a mapping's path/value, never another annotation's literal."""
    match = re.search(
        r"(?:Request|Get|Post|Put|Delete|Patch)Mapping(?:\s*\(([^)]*)\))?", annotations
    )
    if match is None:
        return ""
    arguments = (match.group(1) or "").strip()
    named = re.search(r"\b(?:path|value)\s*=\s*(.*?)(?:,\s*\w+\s*=|$)", arguments)
    expression = (
        named.group(1).strip()
        if named
        else arguments
        if arguments.startswith(('"', "'", "{"))
        else ""
    )
    if not expression:
        return ""
    literal = re.fullmatch(r"[\"']([^\"']*)[\"']", expression)
    return literal.group(1) if literal else None


def _http_method(annotations: str) -> str:
    for marker, verb in (
        ("GetMapping", "GET"),
        ("PostMapping", "POST"),
        ("PutMapping", "PUT"),
        ("DeleteMapping", "DELETE"),
        ("PatchMapping", "PATCH"),
    ):
        if marker in annotations:
            return verb
    request_method = re.search(r"RequestMethod\.([A-Z]+)", annotations)
    return request_method.group(1) if request_method else "ANY"


def _mq_contract(broker: str, topic: str, *, namespace: str = "", event_type: str = "") -> str:
    return f"mq:{broker}:{namespace}:{topic}:{event_type}"


# 优先处理明确 RPC 类型，HTTP 前缀或局部路径未知时不发布猜测契约。
def _endpoint_contract(
    node: SemanticNode,
    annotations: str,
    owner_annotations: str,
    *,
    owner_is_dubbo: bool,
    owner_is_grpc: bool,
    method_is_grpc: bool,
    rpc_qualified_name: str = "",
) -> str:
    if owner_is_dubbo:
        return _dubbo_contract(rpc_qualified_name or node.qualified_name, owner_annotations)
    if owner_is_grpc or method_is_grpc:
        return _grpc_contract(node.qualified_name)
    method = _http_method(annotations)
    prefix, path = _mapping_route(owner_annotations), _mapping_route(annotations)
    if prefix is None or path is None:
        return ""
    route = _normalized_route(prefix.rstrip("/") + "/" + path.lstrip("/"))
    return f"http:{method}:{route}"


def _source_scope(file: str) -> str:
    normalized = file.replace("\\", "/").strip("/")
    marker = "/src/"
    if marker in normalized:
        return normalized.split(marker, 1)[0]
    return normalized.rsplit("/", 1)[0] if "/" in normalized else "."


# 只在源码范围内存在唯一部署候选时认领，多个候选保持未分配。
def _select_deployable(
    candidates: list[dict[str, Any]],
    source_file: str,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    source_scope = _source_scope(source_file)
    ranked = [
        item
        for item in candidates
        if source_scope == str((item.get("attributes") or {}).get("sourceScope") or "")
    ]
    return ranked[0] if len(ranked) == 1 else None


# 类型索引包含分析上下文，同名类型在不同构建中保持隔离。
def _type_id_lookup(facts: SemanticFacts) -> dict[tuple[str, str, str], str]:
    """Index type-node IDs by (language, qualified name, analysis context ID)."""
    return {
        (item.language, item.qualified_name, getattr(item, "analysis_context_id", "")): item.id
        for item in facts.nodes
        if item.kind == "type"
    }


def _resolve_owner(
    item: SemanticNode,
    type_lookup: Mapping[tuple[str, str, str], str],
) -> str:
    """Return the owning type's node ID, or the raw owner string when unindexed."""
    raw = _owner(item)
    return type_lookup.get(
        (
            item.language,
            raw.removeprefix(item.language + ":"),
            getattr(item, "analysis_context_id", ""),
        ),
        raw,
    )


# (kind, relation, resource_name, framework, tier, nameResolvedFrom)
_Classification = tuple[str, str, str, str, str, str]


# 完整静态值可命名非数据库资源，SQL 参数不能当作数据库名称。
# 基于关键词的分类保持启发式等级，即使调用目标已精确解析。
def _builtin_classifications(
    call: SemanticEdge,
    caller_node: SemanticNode,
    target_annotation_items: tuple[str, ...],
    lowered_target: str,
    summary: ValueSummary,
) -> list[_Classification]:
    """Match built-in resource categories against one call target.

    `summary` names non-db resources when complete; incomplete summaries fall
    back to a dynamic-<kind> placeholder. Returns classification rows only.
    """
    classifications: list[_Classification] = []
    for category in _BUILTIN_RESOURCE_CATEGORIES:
        framework = category.matched_framework(lowered_target)
        if framework is None:
            continue
        relation = category.relation_of(call.operation)
        # SQL text and arbitrary query arguments are not database names.
        selected = (
            summary
            if category.resource_kind != "db"
            else ValueSummary(reason="database-binding-unknown")
        )
        names = selected.values if selected.complete else ("dynamic-" + category.resource_kind,)
        resolved_from = selected.reason
        tier = (
            "annotation-verified"
            if category.annotation_markers
            and _has_annotation(
                (*_annotations(caller_node), *target_annotation_items),
                category.annotation_markers,
            )
            and _annotation_resolution((*_annotations(caller_node), *target_annotation_items))
            == "exact"
            else "keyword-heuristic"
        )
        classifications.extend(
            (category.resource_kind, relation, name, framework, tier, resolved_from)
            for name in names
        )
    return classifications
