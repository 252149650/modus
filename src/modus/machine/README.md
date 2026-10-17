<!--
本层的产物可重建，不能承担人工知识的持久所有权。
输入身份贯穿六阶段，局部缓存命中不能绕过基线核验。
阶段回执与覆盖缺口分别保留，消费者据此判断证据范围。
扩展观察器时同步实现身份、能力声明及缓存失效依据。
公开API返回有界结果，不把内部图结构泄漏为稳定协议。
进度只报告已完成工作，不预言后续阶段成功。
可选工具不可用可以降级，身份漂移和损坏则必须阻止发布。
安装包中的资源访问与源码工作区保持同一契约。
修改调用图时回查各子包的所有权说明。
-->

# 初始化分析层

分析层把冻结的仓库转换为有界、经过身份核验的创作证据。产物可重建、可丢弃；经过评审的项目知识才承载长期结论。

## 数据流与所有权

```text
仓库 + 配置 + 工具链
        ↓
corpus.freeze → facts.observe → resolution.resolve
     N1              N2                 N3
                                        ↓
packet.package ← projection.project ← capability.compose
      N6                N5                    N4
```

依赖只沿 `corpus ← facts ← resolution ← capability ← projection ← packet` 向左。`store` 与 `runtime` 是横切支撑，不导入语义层。`pipeline.py` 只拥有编排，让六个生命周期调用在同一屏内可见。

N2 完成所有源码观察并发布带类型的证据。N5 消费 N3/N4，不读取快照、解析入口或调用 N2 辅助函数。N6 核验、约束预算、查询与发布，不创造新语义事实。

| 层 | 拥有 | 不得拥有 |
| --- | --- | --- |
| N1 Corpus | 冻结字节、来源角色、语料/上下文/基线摘要 | 语法与行为推断 |
| N2 Facts | 结构、编译器、规则观察，类型化证据与覆盖回执 | 跨观察器身份合并 |
| N3 Resolution | 身份绑定、带类型关系、拓扑与未解析结果 | 业务能力结论 |
| N4 Capability | 有预算的公开入口到终止效果切片 | 持久化与领域决策 |
| N5 Projection | 能力优先的建议、视图、省略项与展开句柄 | 快照读取与源码解析 |
| N6 Packet | 完整性、原子发布、查询与上下文预算 | 新语义观察 |

阶段结果不可变，只携带本层产物、`baseline_digest` 与规范回执，不嵌套整个上游对象。解析度与完整度可以保持或下降，不能悄然升级。省略细节以数量与精确展开句柄呈现。

## 消费者 API

```python
from modus.machine import (
    AnalysisIdentity,
    AnalysisOptions,
    ContextRequest,
    EvidenceQuery,
    analyze_repository,
    build_authoring_context,
    open_analysis,
    query_evidence,
)

receipt = analyze_repository(root, AnalysisOptions())
bundle = open_analysis(
    root,
    AnalysisIdentity(receipt.bundle_digest, receipt.baseline_digest),
)
page = query_evidence(bundle, EvidenceQuery(view="evidence"))
packet = build_authoring_context(bundle, ContextRequest(...))
```

包根只导出消费者入口、请求/结果契约和 `MachineError` 层级。阶段实现从各自阶段包导入契约。`AnalysisReceipt`、`EvidencePage`、`AuthoringContextPacket` 和 `CapabilitySlice` 在 `schemas/machine` 下有严格 JSON Schema。证据包基线身份统一使用 `baselineDigest`，单个源码见证使用 `sourceDigest`。

## 进度与可观察性

[`progress.py`](progress.py)定义稳定事件与节流报告器。进度只用于观察，不能影响证据、身份摘要或错误语义。顶层暴露 N1–N6；N6 细分输入核验、可移植性、安全检查、目录构建、证据核验、原子发布与清理。

协议事件保持稳定：

```text
package.catalog.evidence-write progress=16384/350450 unit=records elapsedMs=21840
```

CLI 将其展示副本转为中文并写入 stderr：

```text
初始化分析：整理证据（package.catalog.evidence-write） · 进度 16384/350450 条记录 · 耗时 21840 毫秒
```

stdout 留给最终 JSON 回执。阶段计时位于 `timings.packageStages`，不进入摘要。这样可分辨缓慢但持续推进的工作与失败，且不削弱确定性发布。

## 扩展

`language-registry.json` 是声明入口。不可变 N2 注册表调度 `StructuralExtractor`、`CompilerProvider`、`EntryDetector`、`BoundaryResolver`；观察开始前拒绝未知名称、重复注册、缺失版本以及声明/实现不一致。

新增语言需在同一变更中：

1. 声明后缀、提取器、能力层级、提供方、边界解析器与入口检测器。
2. 在 `facts/extension_registry.py` 注册具名实现。
3. 增加夹具，证明文件发现、缺失工具链覆盖回执，以及受支持时的入口到效果投影和上下文输出。

需要新解析器或内置提供方时，同步依赖、资源、锁文件与发布检查。例如 Go 声明 `extract_go`、`go` 编译器提供方、`builtin-go-frameworks` 和入口检测器；Go 工具链缺失只降低 Go 语义覆盖，结构与通用文本事实仍可用，整体分析继续。

## 失败与发布

硬失败使用 `MachineError` 与稳定的 `machine.<owner>.<kebab-reason>` 错误码。契约、完整性、漂移、预算、配置、必需提供方与内部不变量失败中止操作；可选提供方失败输出覆盖回执。

持久化诊断只包含稳定代码、安全摘要和脱敏的仓库相对坐标，不能记录 `str(exc)`、绝对路径、凭据、身份号码或原始源码字面量。终端中文恢复建议不参与该协议。

发布包不可变、按内容寻址、原子写入，打开前核验。游标绑定证据包与规范查询。只接受 `schemaVersion: 1` 缓存形态，旧形态拒绝后重建，不迁移。

依赖方向由[架构测试](../../../tests/machine/test_architecture.py)检查；另见[项目架构](../../../ARCHITECTURE.md)、[内部架构](ARCHITECTURE.md)与[生命周期契约](../../../docs/knowledge-lifecycle-boundary.md)。
