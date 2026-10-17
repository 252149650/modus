# 当前知识与源码检索、任务编排

## 路径与所有者

`cli_commands/context.py → commands/context_cmd.py → orchestrator/context_resolver.py: resolve_context → retrieval/index.py/pipeline.py → delivery.py`。workspace 有独立分派；普通请求只允许 `project-read`。

| 位置 | 责任 |
| --- | --- |
| `orchestrator/preflight.py` | 安装和知识健康观察，不阻止未接入项目的源码工作 |
| `path_scope.py`、`search_policy.py` | 路径、遍历、输出排除、敏感值脱敏 |
| `evidence_search.py`、`fallback.py`、`query_terms.py` | ripgrep/Python 搜索、兼容入口、多语言定位词 |
| `retrieval/query.py` | 意图、证据需求、显式路径、工作区成员选择 |
| `index.py`、`stages/discovery.py`、`selection.py` | 当前 Markdown 的安全发现与渐进选择 |
| `stages/documents.py` | 文档角色、状态、标题、父上下文、语义条目及链接 |
| `stages/lexical.py`、`ranking.py` | 多语言分词/BM25、结构信号与倒数排名融合、稳定同分排序 |
| `routing.py`、`pipeline.py` | Domain/Module 推断、源码锚点核验、回退与证据组装 |
| `budget.py`、`settings.py`、`delivery.py` | 一个截止时间、上下文上限、公平采集、覆盖提示与最终裁剪 |
| `orchestrator/knowledge_sync.py` | 源码变化→待评审知识候选，详见 [知识指南](knowledge-publication.md) |

## 有界流程

明确符号/路径时直接深入；不明确时从系统概览到 Domain router，再到模块/专题，最后回当前源码。索引活在本次请求中，无需向量数据库或持久旧索引。

初排推断路由，再重排相关正文、父导航、规则/约束及源码。推断领域主要影响排序，显式路径/成员范围是过滤边界。知识-only 显式范围不能偷偷授权全仓源码搜索；无效/旧前缀不能扩大搜索范围。

有效源码锚点优先；不足时使用有界 rg/Python 回退。回退共用文件策略、脱敏和截止时间，命令失败形成结构化状态，不改变问题含义。扫描不跟随符号链接，也排除 Modus 自己的生成目录。

正文交付保留祖先标题和同一逻辑条目的条件、失败/恢复上下文；不只截取孤立命中行。裁剪后一起重建覆盖提示、readTarget 和文本。覆盖的语义是 `retrieval-hint-only`，不是事实核验或修改授权。

当前默认：cold_budget_ms 3000、max_context_chars 12000、max_members 3、max_candidates 24、max_body_sections 6；以 `RetrievalSettings` 和有效项目配置为准。全部阶段/成员共享同一个 RetrievalBudget，可选回退不能重置时钟。超时与候选截断明确为 partial/缺口。

## 修改入口

新检索信号改 ranking 所有者，新证据源改 pipeline，交付策略改 delivery，搜索后端改 evidence_search 并复用 search_policy。`runtime.py`、`stages/sections.py` 是兼容 facade，不作为新逻辑堆放位置。

测试：`test_knowledge_retrieval.py`、`test_orchestrator.py`、`test_cli_boundaries.py`、`test_workspace_lifecycle.py`。重点是当前内容而非旧缓存、时间预算、显式范围、entry 上下文、确定性、部分结果、rg 回退、脱敏和无知识仍可读源码。
