# 多仓工作区与外层导航

## 明确工作区身份

仅当有效 `.modus/config.yaml` 声明 `kind: workspace` 才启用工作区行为，不能根据“当前目录有多个子仓”猜测。成员由用户确认，至少两个，路径规范且位于外层根内，身份/路径唯一。

核心入口为 `src/modus/workspace.py`：discover_members、normalize_members、inspect_workspace_knowledge、validate_adoptable_member、resolve_context、refresh_generated、sync_knowledge。`workspace_maps.py` 只渲染已观察成员，不读写成员仓库。

## 主要链路

安装 `commands/init_project.py: run_workspace_init` 先规范成员，再 `run_init(kind=workspace)` 写外层资产；成员返回 referenced/readOnly 及安装/知识状态，不隐式初始化成员。

外层知识 router 为 `modus/knowledge/handbook/SKILL.md`。生成地图位于 `handbook/references/generated`，有 member/system/capability/service/feature/contract/object/infrastructure/delivery/fact-status 及成员卡；路径名单由 `paths.py` 拥有。它们是导航投影，成员自身 Domain/Module 的知识主定义仍在成员仓库。

`refresh_generated` 从当前成员只读观察生成外层地图；不可用成员有具体原因卡片，结果可 partial。外层写入自己的生成目标仍用读取时哈希和知识写入会话。

`resolve_context` 按任务/显式路径选成员，把路径换成成员相对路径，然后逐成员请求并组合外层证据。成员共用同一个 RetrievalBudget，不能每仓重置计时或忽略未查询成员。失败进入 unavailableMembers，不把部分可用当全工作区完整。

`sync_knowledge` 按变更路径前缀分派只读候选，返回路径重新加外层前缀；未分派变化在外层处理。它不能据此修改成员正文、扩大范围或代替助手语义判断。

## 隔离约束

外层 init/update/uninstall、地图刷新和状态盘点不写成员源码、配置、清单或知识。初始化缺失成员是助手显式创作流程中的单独已授权动作，不能藏入导航生成服务。

外层不是单个分析语料根；`machine/pipeline.py` 拒绝 workspace-root 构建。要为创作取得机器证据，分别确认成员后独立分析，不能把多个仓库混成共享无边界图。

修改地图内容先改纯渲染者；成员选择/路径/读取失败改 workspace 服务或 retrieval query；成员知识身份改 scope/metadata 所有者。外层业务材料与生成导航同样有不同人工/机器所有权。

测试：`test_workspace_lifecycle.py`、`test_knowledge_retrieval.py`、`test_project_lifecycle.py`、`test_install_safety.py`。证明 outer-only 写入、卸载隔离、live 成员变化、坏成员 partial、显式路径分派和共享预算。
