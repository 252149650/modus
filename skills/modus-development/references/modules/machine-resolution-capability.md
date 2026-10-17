# N3–N4：身份关系、拓扑与能力切片

## 源码与职责

| 模块 | 责任 |
| --- | --- |
| `machine/resolution/lifecycle.py: resolve` | 结构图与编译事实协调、拓扑隔离、引用结果、基线绑定 |
| `semantic_reconcile.py`、`reference_outcomes.py` | 上下文内身份绑定、非破坏对应、冲突和未解析引用 |
| `evidence_graph.py`、`method_graph.py`、`navigation.py` | 私有 keyed graph、方法关系与有界导航 |
| `topology*.py` | 组装、类型/归属规则、记录、资源、连续见证和身份检查 |
| `view_quarantine.py` | 隔离无效可选视图，保留原因，避免坏视图污染可用事实 |
| `machine/capability/lifecycle.py`、`composer.py` | N4 组合入口；消费 ResolvedProgram |
| `input_normalization.py`、`seed_selection.py`、`traversal.py` | 统一输入、代表入口选择、有预算的入口到终止效果遍历 |
| `capability/contracts.py`、`composition_model.py` | 能力、效果、路径状态及稳定身份 |

## 真实链路

N3 先构建结构证据图，再协调编译语义。协调失败重新建立未污染的结构图，记录 unavailable；拓扑失败有独立降级，不等于丢弃全部结构事实。动态分派、同名身份、外部边界不能悄然提升到 exact。

N4 从有证据的公开触发器出发，沿允许的带类型边遍历，到权威写入、明确输出/交接等终止效果停止。它保留 unresolved、循环、截断及运行边界，不把每次库调用视为业务效果，不跨未闭合边界拼成完整运行链路。

入口过多时 `seed_selection` 优先 HTTP/RPC/MQ/job/CLI 等明确触发，再按目录轮询，防止大目录独占样本；未裁剪时保持输入顺序。`uncovered_entry_ids` 与 omission 是结果的一部分，代表路径不证明穷举。

能力 identity 由语义入口签名等稳定内容决定，行号漂移不应制造一个全新能力。证据解析度与完整度只保持或降低；使用 `states.py` 的 floor，不新增本地 confidence 枚举。

## 开发判断

N3 绑定现有观察，N4 组合已有证据；框架入口、资源信号、源码值缺失应修 N2，不能在 N4 临时重读源码补齐。业务名称和 Domain/Module 归属属于 K 侧，能力组合不使用 LLM。

拓扑边需要来源和上下文连续性。依赖声明、import、候选 call、聚类邻近分别支持不同强度的结论，不能用“有边”代替运行时必然调用。新增效果类别应定义停止含义、权威对象、失败/恢复和边界，而不是仅匹配字符串。

验证：`machine/test_capability_composer.py`（HTTP→写入、行号漂移、测试源码排除、样本公平）、`test_lifecycle_degradation.py`（独立降级）、`test_states.py`、`test_architecture.py`、`test_contracts.py`、`test_analysis_pipeline.py`。复杂真实语言链路同时跑语言夹具。
