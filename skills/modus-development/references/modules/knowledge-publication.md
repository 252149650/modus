# 知识身份、写入、增量维护与核验

## 所有者地图

| 位置 | 责任 |
| --- | --- |
| `paths.py`、`knowledge_identity.py` | 规范路径、物理分类与 human/business/machine/mixed 所有权 |
| `scope_models.py`、`scope_resolver.py` | Domain/Module ID、confirmed/observed 等状态、realization、源码锚点、Markdown 范围重建 |
| `skillmd.py`、`knowledge_reader.py` | 有界 frontmatter、安全描述符绑定读取 |
| `knowledge_write_session.py: write_document/delete_document` | 每文件锁、CAS、原子替换、人工保护、精确生成文档退役 |
| `knowledge_update.py: update_knowledge` | 全请求形状先验证，逐文件写入、结果分类、延迟退役 |
| `orchestrator/knowledge_sync.py: inspect_knowledge_impact` | 源码变化与模块锚点关联，只读候选/冲突/丢失锚点 |
| `knowledge_seeds.py`、`commands/seed_business.py` | 精确种子识别、只补缺的人工业务索引 |
| `commands/verify_knowledge.py`、`verify_anchors.py` | 检查器组合与只读结果 |
| `knowledge_verification/` | discovery、structure、metadata、markdown_integrity、authoring_card、publication |

## 原子性与权限

写入完整读取目标并记录 SHA-256，锁内重读/CAS，再原子替换；expected_hash=None 明确表示预计不存在。不要刷新哈希后盲目覆盖，冲突需要重新理解新内容。

人工约束、business 和其他保护类别默认禁止自动同步。Team Standard 有更窄策略：首次初始化仅缺失/精确框架种子可替换，既有团队正文需要明确维护授权。受管 manifest 不能把人工意图变成机器所有权。

`update_knowledge` 在首个副作用前校验批次形状，逐文件记录 writes/unchanged/conflicts；有写入冲突时延迟全部 deletions，已经成功的独立写入不回滚。status 是 completed/partial/noop，不能把“请求已受理”当整批完成。

删除仅接受明确生成文档路径与观察到的哈希，不接受 glob。缺失是幂等终态，人工文档不能因为符号消失就退役。锁、inode/路径重绑能检测已观察替换，但 POSIX 不提供对外部编辑器绝对原子的 compare-and-unlink；避免声称全环境无竞态。

## 候选与核验

`knowledge sync`/兼容 sync_knowledge 只返回候选，不写正文。explicit/symbol/path-only 的置信度与 anchorReviewCandidates 是定位线索，助手必须语义判断。validation=failed 可维护既有事实/失败记录，不能写未验证行为为成功。

普通 verify 检查常规文件、规范布局、围栏、相对链接/fragment、可移植引用、可选 ModuleMeta 与物理身份、源码锚点、敏感字面量。标题、篇幅、图数量不是质量门槛。

publication 额外消费唯一 `modus/.cache/knowledge-authoring/scope-confirmation.md` 的 schemaVersion 2：逐模块 evidence、域 coverage、reviewed 状态与 changed/unchanged 终态、图表承诺或有理由豁免、结构化锚点和生成正文无源码路径。它核对已声明承诺，不给正文自然语言打分。`clean` 不等于语义准确。

ModuleId 为 `<domain>/<module>`，路径/元数据需一致；移动锚点不改变稳定业务身份。旧知识可从规范路径推断缺失元数据，明确冲突必须报告，不能靠重命名掩盖。

## 验证

测试：`test_knowledge_update.py`、`test_verify_knowledge.py`、`test_knowledge_retrieval.py`、`test_sensitive_literals.py`、`test_orchestrator.py`。覆盖并发、外链/软链、原文件保留、人工内容、部分失败/退役延迟、重试、seed 保留、optional metadata、publication 执行卡和真实锚点漂移。

知识路径/metadata 变化同时读 [创作工作流](authoring-templates.md)，让实现、设计、随包模板和检索行为一致。
