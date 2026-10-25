<!--
路径参与检索、身份和所有权判断，重命名需要同步所有消费者。
目录示例描述归属，不能作为自动覆盖已有文件的授权。
-->

# 稳定布局与所有权

路径是检索、身份和所有权协议，应保持稳定；标题与篇幅是表达选择。

```text
modus/knowledge/
├── architecture-overview.md
├── constraint/
│   ├── global-standard.md
│   └── team-standard.md
└── domain/<domain>/
    ├── SKILL.md
    └── references/
        ├── api.md                      # 接口与数据契约
        ├── downstream.md               # 协作与修改影响
        ├── configuration.md            # 规则与配置分支
        ├── lifecycle.md                # 状态、资源与观测
        ├── modules/<module>.md          # 模块场景手册
        └── business/
            ├── index.md
            ├── meta/index.md
            ├── experience/index.md
            └── scenario/index.md
```

新建 Domain 固定生成四专题：`api.md`、`downstream.md`、`configuration.md`、`lifecycle.md`。它们在域内共用，Module 保持独立场景手册，不为每个模块复制四份专题。确无适用机制时，专题简述已核验范围与不适用原因，不填空表或虚构事实。

旧六专题和 `api/`、`object/` 一级分片继续可读、可检索、可核验；完整旧布局不因软件升级变成不完整。已有域默认沿用原主定义，不自动迁移。确认合并时先建立新落点、修复入链、回读，再按授权和读时哈希退役旧内容，不能同时维护竞争定义。

Module 沿用原元数据契约；专题使用普通 Markdown 和可选源码锚点，不引入新的卡片类型、模块计数或元数据版本。复杂接口与数据可沿用 `api/` 一级分片，入口保留总览；其余专题按语义章节组织。

根概览、Domain、专题和 Module 属于生成正文。`constraint/` 与 `business/` 默认人工所有；初始化种子不把文件永久冻结为固定字节。结构变化先创建新落点并修复入站链接，最后按读时哈希删除已证明退役的生成文件。人工文件只有明确授权时才修改，结构确认最多授权最小链接修复，不授权删除或重写周围语义。

生成正文默认以普通 Markdown 服务开发者阅读。源码位置属于可验证元数据，不属于读者叙事：根概览、Domain、专题和 Module 需要源码追溯时，统一把少量代表性位置写入 frontmatter 的 `anchors.locations`；正文不得出现仓库源码路径、行号或“证据：path#L”清单。正文可以使用对理解和修改确有帮助的业务名称、协议身份与限定符号，但必须解释它们怎样参与任务，不能让读者通过打开源码自行补全含义。

frontmatter 不是隐藏论证仓库。每个位置应代表公开入口、关键判定、权威写入、完成/失败或恢复之一，多个结论由同一位置支撑时复用，不能把调查读取过的所有文件逐项搬入。删除 frontmatter 后，正文仍须完整说明行为、边界、失败恢复和修改验证；只看 frontmatter 也不能替代正文。
