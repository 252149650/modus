"""Language-neutral typed semantic facts emitted by compiler providers.

The wire contract is intentionally JSONL-friendly: every line is one ``node``,
``edge`` or ``diagnostic`` record.  Keeping the provider boundary process based
isolates compiler crashes and version conflicts from the Modus process.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from modus.machine.errors import ProviderError
from modus.machine.facts.semantic_contracts import (
    AnalysisContext,
    CoverageReceipt,
    ObservationDisposition,
)

NODE_KINDS = frozenset(
    {
        "repository",
        "build-unit",
        "deployable-service",
        "component",
        "endpoint",
        "function",
        "method",
        "type",
        "rpc-service",
        "topic",
        "db",
        "table",
        "cache",
        "keyspace",
        "config-key",
        "state-machine",
        "state",
    }
)
RELATIONS = frozenset(
    {
        "contains",
        "implements",
        "injects",
        "calls",
        "exposes",
        "invokes",
        "publishes",
        "consumes",
        "reads",
        "writes",
        "invalidates",
        "configured_by",
        "transitions_to",
    }
)
RESOLUTIONS = frozenset({"exact", "candidate", "heuristic", "unresolved"})
BINDING_KINDS = frozenset(
    {
        "declared-exact",
        "runtime-exact",
        "runtime-candidate",
        "heuristic",
        "unresolved",
    }
)
ANALYSIS_COMPLETENESS = frozenset({"complete", "partial", "fallback"})
LANGUAGES = frozenset(
    {"python", "go", "java", "cpp", "javascript", "typescript", "tsx", "generic", "external"}
)


# 身份摘要只使用调用方提供的稳定材料，不读取环境或随机状态。
# 前缀区分命名空间，字段分隔符避免简单拼接的边界歧义。
def stable_id(prefix: str, *parts: object) -> str:
    body = "\x1f".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]}"


# 禁止 NaN 等非标准 JSON 数值，保证跨进程协议可互操作。
# 排序与紧凑编码同时用于冲突比较和内容摘要。
def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


# 所有提供者统一使用一基位置，并显式声明列编码。
# UTF-8 字节、UTF-16 单元和 Unicode 字符不能直接混作同一列号。
# 路径必须相对仓库，提供者不得把宿主绝对路径写入公开事实。
# 缺失结束位置在序列化时回落起点，保持兼容的完整位置形状。
@dataclass(frozen=True)
class SourceAnchor:
    file: str
    line: int
    column: int = 1
    end_line: int | None = None
    end_column: int | None = None
    column_encoding: str = "unknown"

    def __post_init__(self) -> None:
        if not self.file or self.file.startswith("/") or ".." in self.file.split("/"):
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "source anchor file must be a repository-relative path",
            )
        if self.line < 1 or self.column < 1:
            raise ProviderError(
                "machine.facts.invalid-provider-record", "source anchor positions are one-based"
            )
        if self.end_line is not None and self.end_line < self.line:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "source anchor end line may not precede its start",
            )
        if self.end_column is not None and self.end_column < 1:
            raise ProviderError(
                "machine.facts.invalid-provider-record", "source anchor end column is one-based"
            )
        if self.column_encoding not in {"unknown", "utf8-byte", "utf16", "unicode-codepoint"}:
            raise ProviderError(
                "machine.facts.invalid-provider-record", "unsupported source column encoding"
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file": self.file.replace("\\", "/"),
            "line": self.line,
            "column": self.column,
            "columnEncoding": self.column_encoding,
        }
        result["endLine"] = self.end_line if self.end_line is not None else self.line
        result["endColumn"] = self.end_column if self.end_column is not None else self.column
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceAnchor:
        return cls(
            file=str(value["file"]),
            line=int(value["line"]),
            column=int(value.get("column", 1)),
            end_line=int(value["endLine"]) if value.get("endLine") is not None else None,
            end_column=(int(value["endColumn"]) if value.get("endColumn") is not None else None),
            column_encoding=str(value.get("columnEncoding") or "unknown"),
        )


# 提供者名称、版本与上下文摘要共同解释观察来源。
# 同一工具在不同构建上下文产生的结果不能无条件合并。
# 规则包为可选补充，不替代提供者版本的缓存身份。
@dataclass(frozen=True)
class Provenance:
    provider: str
    provider_version: str
    context_digest: str
    rule_pack: str = ""

    def __post_init__(self) -> None:
        if not self.provider or not self.provider_version:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "provider and provider version are required",
            )
        if len(self.context_digest) != 64 or any(
            item not in "0123456789abcdef" for item in self.context_digest
        ):
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "context digest must be a lowercase SHA-256 digest",
            )

    def to_dict(self) -> dict[str, str]:
        result = {
            "provider": self.provider,
            "providerVersion": self.provider_version,
            "contextDigest": self.context_digest,
        }
        if self.rule_pack:
            result["rulePack"] = self.rule_pack
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Provenance:
        return cls(
            provider=str(value["provider"]),
            provider_version=str(value["providerVersion"]),
            context_digest=str(value["contextDigest"]),
            rule_pack=str(value.get("rulePack", "")),
        )


# 观察 ID 与符号 ID 分离，支持同一符号在多个构建条件下出现。
# 节点可具有多个源码锚点，例如前向声明与定义。
# 构建单元是必需归属，外部声明也需说明由哪个分析单元观察。
# 完整度独立于节点种类，存在声明不等于获得完整语义。
@dataclass(frozen=True)
class SemanticNode:
    id: str
    kind: str
    language: str
    qualified_name: str
    build_unit_id: str
    source_anchors: tuple[SourceAnchor, ...] = ()
    deployable_id: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None
    symbol_id: str = ""
    analysis_completeness: str = "complete"
    analysis_context_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in NODE_KINDS:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported semantic node kind: {self.kind}",
            )
        if self.language not in LANGUAGES:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported semantic node language: {self.language}",
            )
        if not self.id or not self.qualified_name or not self.build_unit_id:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "semantic node id, qualifiedName and buildUnitId are required",
            )
        if self.analysis_completeness not in ANALYSIS_COMPLETENESS:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported analysis completeness: {self.analysis_completeness}",
            )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "recordKind": "node",
            "id": self.id,
            "kind": self.kind,
            "language": self.language,
            "qualifiedName": self.qualified_name,
            "buildUnitId": self.build_unit_id,
            "symbolId": self.symbol_id or self.id,
            "analysisContextId": self.analysis_context_id,
            "analysisCompleteness": self.analysis_completeness,
            "sourceAnchors": [item.to_dict() for item in self.source_anchors],
            "attributes": dict(self.attributes),
        }
        if self.deployable_id:
            result["deployableId"] = self.deployable_id
        if self.provenance is not None:
            result["provenance"] = self.provenance.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SemanticNode:
        return cls(
            id=str(value["id"]),
            kind=str(value["kind"]),
            language=str(value["language"]),
            qualified_name=str(value["qualifiedName"]),
            build_unit_id=str(value["buildUnitId"]),
            source_anchors=tuple(
                SourceAnchor.from_dict(item) for item in value.get("sourceAnchors", ())
            ),
            deployable_id=str(value.get("deployableId", "")),
            attributes=dict(value.get("attributes") or {}),
            provenance=(
                Provenance.from_dict(value["provenance"]) if value.get("provenance") else None
            ),
            symbol_id=str(value.get("symbolId") or value["id"]),
            analysis_context_id=str(value.get("analysisContextId", "")),
            analysis_completeness=str(value.get("analysisCompleteness", "complete")),
        )


# 声明目标精度与运行时派发精度分别记录，不能用 exact 混同二者。
# 兼容 resolution 字段必须与声明精度一致，防止新旧读者得到不同解释。
# 运行时单目标只表示若执行时目标唯一，不证明调用一定发生。
# 绑定种类与派发状态交叉校验，拒绝自相矛盾的提供者记录。
# 调用位置可缺失，保留关系时不能伪造源码坐标。
@dataclass(frozen=True)
class SemanticEdge:
    id: str
    source: str
    target: str
    relation: str
    resolution: str
    operation: str = ""
    call_site: SourceAnchor | None = None
    dispatch: str = ""
    contract_key: str = ""
    evidence: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None
    binding_kind: str = ""
    analysis_completeness: str = "complete"
    analysis_context_id: str = ""
    declaration_resolution: str = ""
    dispatch_resolution: str = ""

    def __post_init__(self) -> None:
        if self.relation not in RELATIONS:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported semantic edge relation: {self.relation}",
            )
        if self.resolution not in RESOLUTIONS:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported semantic edge resolution: {self.resolution}",
            )
        if not self.id or not self.source or not self.target:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "semantic edge id, from and to are required",
            )
        binding_kind = self.binding_kind or _default_binding_kind(self.resolution)
        if binding_kind not in BINDING_KINDS:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported semantic binding kind: {binding_kind}",
            )
        if self.analysis_completeness not in ANALYSIS_COMPLETENESS:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                f"unsupported analysis completeness: {self.analysis_completeness}",
            )
        if self.declaration_resolution and self.declaration_resolution not in RESOLUTIONS:
            raise ProviderError(
                "machine.facts.invalid-provider-record", "invalid declaration resolution"
            )
        if self.declaration_resolution and self.declaration_resolution != self.resolution:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "legacy resolution must mirror declaration resolution",
            )
        if self.dispatch_resolution not in {
            "",
            "not-analyzed",
            "singleton",
            "candidates",
            "unresolved",
            "non-applicable",
        }:
            raise ProviderError(
                "machine.facts.invalid-provider-record", "invalid dispatch resolution"
            )
        expected = {"runtime-exact": "singleton", "runtime-candidate": "candidates"}.get(
            binding_kind
        )
        if expected and self.dispatch_resolution and self.dispatch_resolution != expected:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "binding kind conflicts with dispatch resolution",
            )

    @property
    def declaration_precision(self) -> str:
        """Precision of the statically named declaration, independently of dispatch."""
        return self.declaration_resolution or self.resolution

    @property
    def dispatch_precision(self) -> str:
        """A singleton is conditional on execution; it is never a must-execute claim."""
        return self.dispatch_resolution or {
            "runtime-exact": "singleton",
            "runtime-candidate": "candidates",
            "unresolved": "unresolved",
        }.get(self.binding_kind, "not-analyzed")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "recordKind": "edge",
            "id": self.id,
            "from": self.source,
            "to": self.target,
            "relation": self.relation,
            "resolution": self.resolution,
            "bindingKind": self.binding_kind or _default_binding_kind(self.resolution),
            "analysisContextId": self.analysis_context_id,
            "declarationResolution": self.declaration_precision,
            "dispatchResolution": self.dispatch_precision,
            "analysisCompleteness": self.analysis_completeness,
            "operation": self.operation,
            "dispatch": self.dispatch,
            "contractKey": self.contract_key,
            "evidence": list(self.evidence),
            "attributes": dict(self.attributes),
        }
        if self.call_site is not None:
            result["callSite"] = self.call_site.to_dict()
        if self.provenance is not None:
            result["provenance"] = self.provenance.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SemanticEdge:
        return cls(
            id=str(value["id"]),
            source=str(value["from"]),
            target=str(value["to"]),
            relation=str(value["relation"]),
            resolution=str(value["resolution"]),
            operation=str(value.get("operation", "")),
            call_site=(
                SourceAnchor.from_dict(value["callSite"]) if value.get("callSite") else None
            ),
            dispatch=str(value.get("dispatch", "")),
            contract_key=str(value.get("contractKey", "")),
            evidence=tuple(str(item) for item in value.get("evidence", ())),
            attributes=dict(value.get("attributes") or {}),
            provenance=(
                Provenance.from_dict(value["provenance"]) if value.get("provenance") else None
            ),
            analysis_context_id=str(value.get("analysisContextId", "")),
            declaration_resolution=str(value.get("declarationResolution", "")),
            dispatch_resolution=str(value.get("dispatchResolution", "")),
            binding_kind=str(
                value.get("bindingKind") or _default_binding_kind(str(value["resolution"]))
            ),
            analysis_completeness=str(value.get("analysisCompleteness", "complete")),
        )


# 旧提供者的 exact 只提升为声明精确，不默认声称运行时精确。
# 旧提供者缺少绑定种类时，由解析等级补齐兼容值。
def _default_binding_kind(resolution: str) -> str:
    return {
        "exact": "declared-exact",
        "candidate": "runtime-candidate",
        "heuristic": "heuristic",
        "unresolved": "unresolved",
    }[resolution]


# 诊断独立于节点和边，失败也可形成有效提供者输出。
# subject 保留具体文件或符号归属，供后续计算局部覆盖缺口。
@dataclass(frozen=True)
class SemanticDiagnostic:
    code: str
    message: str
    severity: Literal["info", "warning", "error"]
    provider: str
    build_unit_id: str = ""
    subject: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "recordKind": "diagnostic",
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "provider": self.provider,
            "buildUnitId": self.build_unit_id,
            "subject": self.subject,
        }


# 事实集合同时携带上下文、处置回执和覆盖回执。
# 执行是否成功、记录是否接纳和证据是否完整分别表达。
# 不可变集合便于各阶段通过 replace 构造新视图，避免原地覆盖来源。
@dataclass(frozen=True)
class SemanticFacts:
    nodes: tuple[SemanticNode, ...] = ()
    edges: tuple[SemanticEdge, ...] = ()
    diagnostics: tuple[SemanticDiagnostic, ...] = ()
    contexts: tuple[AnalysisContext, ...] = ()
    dispositions: tuple[ObservationDisposition, ...] = ()
    coverage_receipts: tuple[CoverageReceipt, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "contexts": [item.to_dict() for item in self.contexts],
            "dispositions": [item.to_dict() for item in self.dispositions],
            "coverageReceipts": [item.to_dict() for item in self.coverage_receipts],
        }

    # 临时镜像路径回写经过同一 JSONL 解析契约，保证转换后仍有效。
    # 路径归一化由结果类型承担，语料层无需导入事实层结构。
    def rewritten(self, old: str, new: str) -> SemanticFacts:
        """Return these facts with every serialised occurrence of a path swapped.

        Used by mirror-based provider execution to strip disposable mirror
        identities. Living on the result type keeps path normalisation next
        to the serialisation it depends on, so callers (including N1's
        snapshot governor) need no fact-layer imports.
        """
        records = [
            *(item.to_dict() for item in self.nodes),
            *(item.to_dict() for item in self.edges),
            *(item.to_dict() for item in self.diagnostics),
            *(item.to_dict() for item in self.contexts),
            *(item.to_dict() for item in self.dispositions),
            *(item.to_dict() for item in self.coverage_receipts),
        ]
        return parse_jsonl(
            json.dumps(record, ensure_ascii=False).replace(old, new) for record in records
        )


# 每行独立分派记录类型，空行可忽略但未知类型必须失败。
# 错误报告携带行号，便于定位子进程输出协议问题。
# 完整读取后统一规范化，跨行身份冲突不能靠最后一条覆盖。
# 诊断严重度使用封闭集合，未知值不能静默当作警告。
# 逐行协议允许定位单条记录故障，归一化层继续负责跨记录一致性。
def parse_jsonl(lines: Iterable[str]) -> SemanticFacts:
    nodes: list[SemanticNode] = []
    edges: list[SemanticEdge] = []
    diagnostics: list[SemanticDiagnostic] = []
    contexts = []
    dispositions = []
    receipts = []
    for position, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "provider output contains invalid JSON",
                details={"line": position},
            ) from exc
        if not isinstance(value, Mapping):
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "provider records must be objects",
                details={"line": position},
            )
        kind = value.get("recordKind")
        if kind == "node":
            node = SemanticNode.from_dict(value)
            nodes.append(node)
        elif kind == "edge":
            edge = SemanticEdge.from_dict(value)
            edges.append(edge)
        elif kind == "diagnostic":
            severity = str(value.get("severity", "warning"))
            if severity not in {"info", "warning", "error"}:
                raise ProviderError(
                    "machine.facts.invalid-provider-record",
                    "provider diagnostic severity is invalid",
                    details={"line": position},
                )
            diagnostics.append(
                SemanticDiagnostic(
                    code=str(value.get("code", "machine.facts.unknown")),
                    message=str(value.get("message", "")),
                    severity=cast(Literal["info", "warning", "error"], severity),
                    provider=str(value.get("provider", "unknown")),
                    build_unit_id=str(value.get("buildUnitId", "")),
                    subject=str(value.get("subject", "")),
                )
            )
        elif kind == "analysis-context":
            contexts.append(AnalysisContext.from_dict(value))
        elif kind == "disposition":
            dispositions.append(ObservationDisposition.from_dict(value))
        elif kind == "coverage-receipt":
            receipts.append(CoverageReceipt.from_dict(value))
        else:
            raise ProviderError(
                "machine.facts.invalid-provider-record",
                "provider record kind is unknown",
                details={"line": position},
            )
    return normalize_facts(
        SemanticFacts(
            tuple(nodes),
            tuple(edges),
            tuple(diagnostics),
            tuple(contexts),
            tuple(dispositions),
            tuple(receipts),
        )
    )


# 显式上下文优先，旧来源摘要仅作为兼容回退。
# 上下文身份参与去重，跨构建同名记录不能互相抵消。
def observation_context(item: SemanticNode | SemanticEdge) -> str:
    """Use the explicit analysis context, falling back to legacy provenance.

    Identities are unique within this context, not across unrelated build actions.
    """

    return item.analysis_context_id or (item.provenance.context_digest if item.provenance else "")


# 先处理节点身份冲突，再检查边端点是否仍被接纳。
# 孤立边生成拒绝回执与诊断，不能留下看似完整的悬空图。
# 端点必须在同一观察上下文存在，不能误连其他构建变体。
# 节点身份冲突隔离后再次检查端点，防止遗留悬空语义边。
def _admit_edges(
    nodes: tuple[SemanticNode, ...],
    candidates: list[SemanticEdge],
    dispositions: list[ObservationDisposition],
    diagnostics: list[SemanticDiagnostic],
) -> tuple[SemanticEdge, ...]:
    """Reject orphan edges after identity conflicts have removed their endpoints."""
    node_keys = {(observation_context(item), item.id) for item in nodes}
    edges = []
    for edge in candidates:
        context = observation_context(edge)
        if (context, edge.source) not in node_keys or (context, edge.target) not in node_keys:
            dispositions.append(
                ObservationDisposition(
                    "edge", edge.id, "rejected", "semantic.missing-endpoint", context
                )
            )
            diagnostics.append(
                SemanticDiagnostic(
                    "semantic.missing-endpoint",
                    f"Edge has no accepted endpoint: {edge.id}",
                    "warning",
                    "semantic-contract",
                    subject=edge.id,
                )
            )
        else:
            edges.append(edge)
    return tuple(edges)


# 完全相同的重传幂等接纳，内容矛盾的同身份观察全部隔离。
# 冲突不按到达顺序决胜，也不挑选字段更多的记录冒充真实值。
# 每条观察均有处置回执，重复与冲突具有不同含义。
# 规范化已有回执时保留幂等性，避免每次读取增加重复统计。
# 最终边集合以接纳节点为边界，覆盖回执仍保留原始分析事实。
def normalize_facts(facts: SemanticFacts) -> SemanticFacts:
    """Quarantine contradictory identities; identical retransmissions are idempotent.

    Processing receipts cover every emitted record. Conflicting payloads are
    never resolved by arrival order or by selecting a cosmetically better node.
    """
    dispositions = list(facts.dispositions)
    diagnostics = list(facts.diagnostics)
    accepted: dict[str, list[Any]] = {"node": [], "edge": []}
    existing = {
        (
            row.record_kind,
            row.record_id,
            row.context_id,
            row.status,
            row.content_digest if row.status == "conflict" else "",
        )
        for row in dispositions
    }
    # 已有重复回执数量用于识别再次规范化的同一批输入。
    # 冲突回执还绑定内容摘要，因此不同冲突载荷可以分别追溯。
    previous_duplicates: dict[tuple[str, str, str], int] = {}
    for row in dispositions:
        if row.status == "duplicate":
            duplicate_key = (row.record_kind, row.record_id, row.context_id)
            previous_duplicates[duplicate_key] = previous_duplicates.get(duplicate_key, 0) + 1
    for kind, records in (("node", facts.nodes), ("edge", facts.edges)):
        groups: dict[tuple[str, str], list[Any]] = {}
        for item in records:
            groups.setdefault((observation_context(item), item.id), []).append(item)
        for (context, identity), items in sorted(groups.items()):
            # 比较完整规范载荷而非少数字段，来源和完整度变化同样有语义。
            # 组内只要存在两个不同载荷，就不能接纳其中任意一个。
            payloads = {canonical_json(item.to_dict()) for item in items}
            conflict = len(payloads) > 1
            if not conflict:
                accepted[kind].append(items[0])
            else:
                diagnostics.append(
                    SemanticDiagnostic(
                        "semantic.identity-conflict",
                        f"Conflicting {kind} observations: {identity}",
                        "warning",
                        "semantic-contract",
                        subject=identity,
                    )
                )
            # A normalization pass is idempotent for its already-receipted inputs.
            for index, item in enumerate(
                sorted(items, key=lambda row: canonical_json(row.to_dict()))
            ):
                digest = hashlib.sha256(canonical_json(item.to_dict()).encode()).hexdigest()
                status = "conflict" if conflict else "accepted" if index == 0 else "duplicate"
                receipt = ObservationDisposition(
                    kind,
                    identity,
                    status,
                    "semantic.identity-conflict"
                    if conflict
                    else "semantic.identical-duplicate"
                    if index
                    else "",
                    context,
                    digest,
                )
                key = (kind, identity, context, status, digest if conflict else "")
                if (
                    status == "duplicate"
                    and index > previous_duplicates.get((kind, identity, context), 0)
                ) or (status != "duplicate" and key not in existing):
                    dispositions.append(receipt)
                    existing.add(key)
    nodes = tuple(accepted["node"])
    edges = _admit_edges(nodes, accepted["edge"], dispositions, diagnostics)
    return SemanticFacts(
        nodes,
        tuple(edges),
        tuple(diagnostics),
        facts.contexts,
        tuple(dispositions),
        facts.coverage_receipts,
    )


__all__ = [
    "ANALYSIS_COMPLETENESS",
    "BINDING_KINDS",
    "LANGUAGES",
    "NODE_KINDS",
    "RELATIONS",
    "RESOLUTIONS",
    "Provenance",
    "SemanticDiagnostic",
    "SemanticEdge",
    "SemanticFacts",
    "SemanticNode",
    "SourceAnchor",
    "canonical_json",
    "parse_jsonl",
    "normalize_facts",
    "observation_context",
    "stable_id",
]
