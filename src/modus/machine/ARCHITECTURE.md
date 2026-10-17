<!--
依赖方向描述语义所有权，不只要求导入图无环。
存储层拥有发布、绑定与完整性核验，消费者不重复实现。
第三方解析树止于观察器边界，公共事实使用统一数据形状。
扩展注册与实现指纹共同决定能力和缓存身份。
预算终止与解析失败有不同回执，不能统一成空结果。
可选能力缺失不扩大其他观察器的事实范围。
结构拆分时同时检查导入路径、指纹和回归入口。
-->

# 分析层内部架构

## 依赖规则

```text
corpus ← facts ← resolution ← capability ← projection ← packet
                 语义层只能向左导入

store      持久化、目录与完整性；不导入语义层
runtime    资源与工具链策略；不导入语义层
pipeline   锁、进度与计时；只调用六个生命周期动词
```

`tests/machine/test_architecture.py` 将依赖规则变为可执行检查。函数内延迟导入同样受约束。

| 层 | 动词 | 拥有的产物 | 不得决定 |
| --- | --- | --- | --- |
| N1 Corpus | `freeze` | 冻结字节、角色、输入与基线身份 | 语法或行为 |
| N2 Facts | `observe` | 源码观察与提供方覆盖 | 身份协调 |
| N3 Resolution | `resolve` | 实体、关系、歧义与拓扑 | 业务能力 |
| N4 Capability | `compose` | 有预算的入口到效果切片 | 持久化与创作 |
| N5 Projection | `project` | 有界创作建议与视图 | 源码解析与快照读取 |
| N6 Packet | `package` | 核验、发布、查询与上下文预算 | 新语义观察 |

每层返回自身产物、`baseline_digest` 与规范回执，不嵌套完整上游对象。跨层契约使用冻结数据类与枚举；无类型字典限制在解析器内部和传输编码边界。

## 内部模块划分

按变化责任拆分，不按任意行数拆分：

- 关系解析：`topology.py` 组装拓扑，`method_graph.py` 投影符号，`topology_model.py` 共享身份，`topology_validation.py` 递归核验不变量。
- 能力组合：分开输入规范化、代表起点选择、有预算遍历与薄组合器。
- 存储：分开目录构建 `bundle_catalogs`、有界目录 I/O `catalog`、发布与经过身份核验的访问。
- 前端观察：框架/运行时检测和文件路由投影独立于长语法访问器。
- 构建单元：`build_contracts.py` 定义传输契约，`build_support.py` 提供安全发现原语，`build_resolvers/` 下各语言拥有独立解析器，`BuildContextResolver` 只协调策略。

消费者先 `analyze_repository`，用 `AnalysisIdentity` 将 `bundle_digest` 与 `baseline_digest` 一起传给 `open_analysis`，再调用 `query_evidence` 或 `build_authoring_context`。证据包契约使用 `baselineDigest`，只有源码锚点使用 `sourceDigest`。

## 扩展注册

N2 只调度不可变内置注册表中的实现：

| 契约 | 责任 |
| --- | --- |
| `StructuralExtractor` | 解析一份冻结源码 |
| `CompilerProvider` | 为一个构建单元输出语义观察 |
| `EntryDetector` | 提升直接观察到的公开入口 |
| `BoundaryResolver` | 闭合源码局部的框架边界 |

`language-registry.json` 按名称与版本引用实现。启动时拒绝重复、缺版本、未知名称或声明不一致；没有隐式入口扫描或第三方插件 ABI。

语言扩展必须可在一个变更中审计：声明后缀与能力坐标、注册实现、提供发现与降级夹具。以 Go 为例，`.go` 映射到 `extract_go`、`go` 提供方、边界解析器和入口检测器；夹具验证入口到效果投影，以及工具链不可用时的覆盖回执。

## 失败语义

| 异常 | 含义 |
| --- | --- |
| `ContractError` | 调用方输入不符合契约 |
| `IntegrityError` | 发布产物被修改或格式损坏 |
| `DriftError` | 冻结后输入发生变化 |
| `BudgetError` | 预算无效或无法满足 |
| `ConfigurationError` | 声明或注册表状态无效 |
| `ProviderError` | 必需提供方失败 |
| `InvariantError` | 内部出现不应发生的状态 |

硬失败携带稳定的 `machine.<owner>.<kebab-reason>` 错误码。可选提供方失败只降低自身覆盖并输出 `CoverageReceipt`。持久化诊断禁止原始异常文本、绝对路径、源码字面量与凭据；保留代码、安全摘要与仓库相对坐标。

## 存储与信任

证据包不可变且按内容寻址。N6 核验冻结基线，构建范围目录，原子写入，验证根后发布。打开时先核验身份与元数据，再读取查询页。游标绑定证据包和规范查询；未知或历史缓存形态以稳定错误拒绝并重建，当前契约为 `schemaVersion: 1`。

## 明确保留的边界

机器层不创作业务知识，也不选择最终领域。结构社区只提供辅助建议。计时、缓存命中、工作线程数、资源配置和宿主路径不进入语义身份。能力组合不接受 LLM 输出。游标耗尽不是消费者的完成条件。
