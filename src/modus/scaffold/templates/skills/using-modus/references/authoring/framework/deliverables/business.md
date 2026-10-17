<!--
人工业务知识由团队维护，自动化只创建缺失导航。
-->

# 人工业务知识契约

`references/business/` 保存团队人工沉淀，默认不由自动流程生成或改写。首次创建正式 Domain 时补齐缺失的四个索引作为普通导航种子；已有索引不要求永久逐字匹配种子，也不以脚本校验文案。

```text
business/
├── index.md
├── meta/index.md        # 术语、对象、口径与规则
├── experience/index.md  # 历史决策、实践与复盘
└── scenario/index.md    # 场景、参与者、流程与异常
```

索引只说明人工内容的放置方式，不保存源码表格、材料链接、证据状态或自动目录清单。用户明确委托编辑某个人工文件时，在授权范围内完整读取、保留无关内容、按读时哈希 CAS 写入并回读。

建议的初始文案保持简洁：根索引说明三个分类及其相对链接；`meta/index.md` 说明术语、对象、口径与规则；`experience/index.md` 说明历史决策、实践、复盘与适用条件；`scenario/index.md` 说明参与者、流程、结果与异常。它们是可由维护者继续编辑的普通 Markdown，不是 verifier 拥有的 canonical bytes。

init 为确认后的新 Domain 调用 `modus knowledge seed-business --domain <slug> --json`。命令只在 `domain/<domain>/references/business/` 创建缺失索引，保留所有已有人工字节；不得用补丁手写路径，也不得创建 `modus/knowledge/business/`。

已确认结构变化最多允许修复人工文档中的失效知识链接；不得借此改写规则、删除/搬迁人工文件或刷新无关索引。用户材料中的事实应进入承担该职责的生成正文，只有用户明确要求人工沉淀时才写入 business。
