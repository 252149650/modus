# 范围建模

Scope 把证据转化为稳定的能力身份，但不把源码结构直接升级为业务结构。按“参与者与结果 → 权威对象和生命周期 → 修改责任 → Module → Domain”建模。

## 成立条件

Module 能以一个或一组紧密相关的任务解释，具有可辨识的触发、输入、判定、结果和修改责任。Domain 围绕同一业务结果、权威对象集合或长期变更责任组织多个 Module。非商业项目使用“能力域”，不虚构商业概念。

通用数据库、缓存、日志、消息传输、框架适配、Controller/DAO 或纯目录集合默认不是 Domain。只有它们拥有独立用户可见结果、权威状态或产品责任时，才提升为正式能力。

## 四类归属

每项候选进入且只进入以下一类：正式 Domain/Module、跨域平台能力、共享基础设施、带理由的排除项。所有公开 API、页面动作、CLI、SDK、任务、消息、数据作业、插件和基础设施操作都必须获得归属、排除或缺口处置。

从首次记录起使用 `domain:<slug>` 和 `module:<domain>/<slug>`。`D1`、`P2` 等按展示顺序产生的编号只属于临时工作，不进入正文、链接或长期身份。

## 结构变化

首次 init 对完整 Domain/Module 结构合并确认一次。reinit 复用稳定身份，只把新增、拆分、合并、移动、改名和已证明退役视为结构变化。源码路径移动、符号改名、锚点漂移、标题本地化和 API/Object 分片变化都不是结构变化。

符号消失、机器 partial 或覆盖缺口不能单独证明能力退役。退役必须回到入口、责任、状态和调用方，证明能力结果已消失或被明确接管。

## 对话中的确认输出

普通项目需要用户确认 Domain/Module 结构时，确认轮次的最终回复必须依次包含：

1. 完整确认卡 `modus/.cache/knowledge-authoring/scope-confirmation.md` 的可点击文件链接。
2. 完整中文总览表；每个正式 Domain 一行，表头固定为 `业务域名称 | 简介 | 功能模块个数 | 列举功能模块中文名`。功能模块使用中文能力名，不得只列 slug、数量或部分示例代替完整表格。
3. 明确说明正式正文尚未写入或本轮结构变化尚未发布。宿主支持 subagent 且至少有两个边界清楚的 Domain 时，提供四个同级选项，不能把并行授权藏在三个选项后的附注：
   1. **确认业务域及功能模块，并按 Domain 使用 subagent 并行撰写（推荐）**
   2. **确认业务域及功能模块，并串行撰写**
   3. **修改或新增业务域及功能模块**
   4. **重新生成业务域及功能模块**

宿主不支持 subagent 或当前范围不能安全拆成至少两个完整 Domain 时，提供三个选项：

1. **确认业务域及功能模块**（按完整范围持续完成正文、自查和发布）
2. **修改或新增业务域及功能模块**
3. **重新生成业务域及功能模块**

只有用户选择并行选项，或此前已经明确授权按 Domain 委派，才允许加载[并行撰写协议](../parallel-domain-authoring.md)；串行确认不隐含委派授权。并行授权只改变执行方式，不扩大确认范围、源码修改、结构变化或外部操作权限。

这是一项可移植的 Markdown 输出合同，不依赖宿主是否提供结构化用户输入工具。宿主支持结构化选项时可以同时使用，但不得省略确认卡链接、完整表格或上述三个语义选项。输出确认后立即结束当前轮次，不把确认问题埋在普通进度消息中。

首次 init 的表格展示完整拟议结构；reinit 或局部更新只在存在真实结构变化时触发确认，但表格仍展示变更后的完整 Domain/Module 结构，避免用户在缺少上下文时批准局部片段。workspace 的成员范围选择遵循 workspace 流程，不套用本节的单仓 Domain/Module 三选项。

## 唯一执行卡

`scope-confirmation.md` 在确认后继续承载本轮计划与进度，不新建平行 receipt。卡片正文保存本轮双摘要（或机器层不可用原因）、公开入口族的归属/共享/排除/缺口、Surface、构建单元/语言、candidate/capability/path/symbol 句柄和主定义说明；YAML frontmatter 保存发布器可核对的精确 Domain/Module 身份、状态、outcome 和图表决定。

frontmatter 使用以下 `schemaVersion: 2` 机器结构；Domain、Module 与 visual id 都使用稳定 slug。Module 的 `evidence` 记录精确 requested scope、selector 与取证模式，Domain 的 `coverage` 记录域级源码覆盖审计。Scope 阶段两者先置 `pending`，不能在批量写文件后补造为已完成。

Scope 阶段的 `visuals` 只记录候选决定：每项同时记录它回答的 `readerQuestion` 和触发它的真实 `mechanism`。已知需要图表时使用 `mermaid` 或 `table` 并先置 `planned`；当前没有候选时使用 `kind: none`、`status: waived` 和具体理由，不能靠空列表表示“评估过”。这不是最终视觉计划，当前 Domain 的 Module 证据闭合后、写正文前必须重新评估并原位更新。

```yaml
---
modus:
  authoring:
    schemaVersion: 2
    executionMode: domain-parallel
    domains:
      order:
        status: pending
        outcome: pending
        coverage:
          status: pending
          mode: machine-context
          selector: "candidate:order OR capability:submit-order"
        modules:
          submit-order:
            status: pending
            evidence:
              status: pending
              mode: machine-context
              requestedScope: module:order/submit-order
              selector: "capability:submit-order"
        visuals:
          - id: order-lifecycle
            kind: mermaid
            target: modus/knowledge/domain/order/references/state_machine.md
            status: planned
            readerQuestion: 订单怎样从受理推进到可见完成？
            mechanism: 多参与方推进持久状态并包含失败恢复
---
```

发布前 Domain 和每个 Module 都必须为 `reviewed`，Domain outcome 必须为 `changed` 或 `unchanged`。Module evidence 必须为 `ready`；机器 context 的 `requestedScope` 必须精确等于当前 Module，源码 fallback 或纯业务材料必须记录原因。Domain coverage 必须为 `audited`，使用源码 selector；机器 coverage 不可用时才使用带原因的 `source-review`。已实现的图表置为 `authored`，并在目标正文中紧邻图表前写 `<!-- modus:visual:<id> -->`；放弃原计划则置为 `waived` 并记录基于证据的 `reason`。这只核对作者已经声明的决定，不按全局数量强迫画图。

图表计划不是“每个 Domain 填一项”的配额。卡片正文应为每项决定记录读者问题、触发它的复杂关系、唯一主落点和选型理由，并区分导航型表格与解释型图表。Module 导航表只能兑现导航目的；若调查发现异步交接、持久状态、多条件决策、对象映射或资源生命周期，仍需在相应主定义重新规划时序、状态、决策、关系或生命周期表达。一个 Domain 可以有多项 visual，也可以在证据证明关系简单时全部具体豁免；“本域已有一张图”不能豁免另一个独立读者问题。

每个 Module 使用 `pending → evidence-ready → authored → reviewed`；Domain 完成时记录 `changed` 或 `unchanged`。无法继续时记录带原因的 `blocked`，但它不是完成态。只有该 Module 的精确 `module-authoring.requestedScope` 已核对并直接回读当前方法体、配置、SQL、测试或资源，且 `evidence.status` 已原位更新为 `ready`，才能标记 `evidence-ready`；一个覆盖多个独立 Module 的整域宽请求不能推进状态。正文写入后先逐 Module 回读，再进入 `authored`；只有三类读者回读和使用源码 selector 的域级 coverage audit 完成并记录为 `coverage.status: audited` 后，才能标记 `reviewed`。不得在批量写完文件后一次性把所有 Module 从 `pending` 改成 `reviewed`。状态是可恢复的编排事实，不是正文质量评分。

每次阶段转换都原位更新这张卡。生成了文件不等于阶段完成；恢复任务时先读卡片和正式正文，再从最早未完成状态继续。并行时只有根作者串行更新执行卡，Domain worker 通过 handoff 提议事实和状态，不并发写卡。预计剩余范围无法保持当前深度时，应在 Domain 边界保存并恢复，不得压缩后续正文赶进度。所有已确认目标都达到 `reviewed` 且有 `changed / unchanged` Domain 终态后，发布阶段才删除卡片；存在 `blocked` 时保留。执行卡丢失或被提前删除时，不得根据生成文件反向拼装一张更小的“全部 reviewed”卡；能从权威来源完整恢复原状态才继续，否则保留阻塞并如实报告。

卡片正文用三个小账本即可，字段可以按正文语言本地化，但语义不得丢失；发布状态以 frontmatter 为准，正文表不得形成第二套冲突状态：

| 证据会话 | 值 |
| --- | --- |
| bundle / baseline | 双摘要，或不可用原因 |
| 正文语言 | 用户要求或判定依据 |
| 当前阶段 | K1～K6 与最近一次完成动作 |

| 入口族 | 处置 | Domain/Module 或共享落点 | 证据/缺口 |
| --- | --- | --- | --- |
| 公开入口族 | 正式 / 平台 / 基础设施 / 排除 / 未决 | 稳定 ID 或非职责 | selector、源码入口或理由 |

| Domain / Module | Surface | 构建单元与语言 | selectors | 主定义/图形计划 | 状态 | 结果或缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| 稳定 ID | 可多选 | 可多项 | candidate/capability/path/symbol | 专题、Module 与唯一主图 | 四阶段之一 | changed/unchanged，或非完成态 blocked 与具体原因 |
