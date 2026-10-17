# CLI、中文体验与个人维护

## 入口与职责

| 位置 | 所有者 |
| --- | --- |
| `src/modus/cli.py: main/root_callback` | 显式命令树、`-C/--directory`、版本短路、已知产物/分析依赖异常边界 |
| `cli_commands/initialize.py`、`update.py`、`uninstall.py`、`discovery.py` | 公开终端适配：参数、平台选择、交互、stdout/stderr、退出码 |
| `cli_commands/analysis.py`、`context.py`、`knowledge.py` | 已发布技能使用的内部命令适配 |
| `cli_commands/common.py`、`presentation.py`、`analysis_display.py` | Modus 自身命令上的帮助、中文错误、确认、分析进度展示 |
| `command_reference.py`、`surface.py` | 帮助语义与工作流身份/调用维度的主定义 |
| `commands/maintenance_scope.py` | requested/effective scope、project/personal/combined 的共享语义 |
| `commands/self_update.py`、`uninstall_self.py` | 个人发行包所有权、uv/pip 选择、版本探测、更新后重执行与卸载 |
| `commands/*_report.py`、`display_labels.py`、`platforms/display.py` | 结构化结果转换为中文；不改变结果字段 |

## 关键行为

根命令无参数等价根帮助；`--version` 在损坏项目配置前短路；`-C` 的 cwd 变更绑定上下文关闭回调，嵌入调用结束后恢复原 cwd。公开面只有五个终端命令，内部 groups 隐藏但可调用。

普通 CLI 不提前导入分析依赖。`main` 将已知产物损坏转为退出 2，已知 machine 依赖缺失转为退出 3；其他异常沿原边界处理，不能泛化为“所有失败都退出同一码”。分析最终 JSON 保持 stdout，中文进度走 stderr。

维护默认 combined；没安装项目时 combined 可退化 personal，显式 project 不能悄然改变意图。不同适配器的默认确认、dry-run、`--yes` 与输入互斥以当前参数注册为准，测试时用 `--project` 限定临时项目，避免操作个人环境。

个人维护依据运行模块/console script 的发行包身份及 uv receipt。运行包可能属于其他工具的依赖，不能因为名字叫 modus 就升级/删除整个宿主 uv tool。不能拿本仓库版本证明用户 PATH 上 CLI 已升级；维护任务分别报告个人版本与项目 frameworkVersion。

## 修改约束

只在展示边界本地化固定框架句式，不修改 Click/Typer 全局类、私有常量，不整段替换输出。字段、命令、参数、枚举、错误码是机器契约。参数帮助来自真实注册；新增公开命令需同步注册、catalog、文档和测试。

验证：`tests/test_cli_boundaries.py`、`test_cli_localization.py`、`test_project_lifecycle.py`、`test_documented_cli.py`。复现帮助、无参数调用、坏输入、流、退出码及与非 Modus Click 命令隔离；个人维护通过注入 runner/临时环境测，不在测试中升级真实用户 CLI。
