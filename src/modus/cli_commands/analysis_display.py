"""将稳定的分析协议投影为中文提示，不改变证据与回执。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..machine.errors import MachineError

_STAGE_LABELS = {
    "freeze": "冻结源码基线",
    "observe": "提取程序事实",
    "resolve": "解析程序关系",
    "compose": "组合能力模型",
    "project": "生成创作投影",
    "package": "打包证据",
    "complete": "分析完成",
    "package.verify-inputs": "核验输入基线",
    "package.safety.sensitive-literals": "检查敏感内容",
    "package.safety.portability": "检查证据可移植性",
    "package.verify-bundle": "核验完整证据包",
    "package.verify-existing-bundle": "核验已有证据包",
    "package.publish": "发布证据包",
    "package.prune": "清理过期证据",
    "package.complete": "证据包已就绪",
}
_UNITS = {"items": "项", "files": "个文件", "values": "个值", "pages": "页", "records": "条记录"}


# 展示读取协议副本，不修改阶段标识或下游统计。
# 未知阶段保留原名，新增打包子阶段无需同步发布终端词典。
def format_progress(observation: str) -> str:
    """转换进度的展示副本，未知阶段保留标识，便于扩展与定位。"""
    stage, _, fields = observation.partition(" ")
    label = _STAGE_LABELS.get(stage, f"整理证据（{stage}）")
    values = dict(field.split("=", 1) for field in fields.split() if "=" in field)
    parts = [label]
    if "progress" in values:
        parts.append(f"进度 {values['progress']} {_UNITS.get(values.get('unit', 'items'), '项')}")
    if "elapsedMs" in values:
        parts.append(f"耗时 {values['elapsedMs']} 毫秒")
    return " · ".join(parts)


# 异常类型决定恢复动作，原始代码帮助脚本和维护者精确定位。
# 延迟导入避免普通帮助路径加载完整分析器。
# 未知分析错误仍保留通用恢复提示，不猜测未经验证的失败原因。
def format_machine_error(error: MachineError) -> str:
    """按失败类型提供恢复动作，稳定错误码继续用于脚本与问题报告。"""
    from ..machine.errors import (
        BudgetError,
        ConfigurationError,
        ContractError,
        DriftError,
        IntegrityError,
        ProviderError,
    )

    if isinstance(error, ConfigurationError):
        message = "分析配置无效，请检查项目目录与 .modus/config.yaml。"
    elif isinstance(error, BudgetError):
        message = "分析预算无效或不足，请调整请求的数量与字节上限。"
    elif isinstance(error, DriftError):
        message = "分析期间源码或配置发生变化，请重新构建本轮证据。"
    elif isinstance(error, IntegrityError):
        message = "证据完整性核验失败，请重新构建并使用新回执。"
    elif isinstance(error, ProviderError):
        message = "必需的分析工具不可用，请检查本地工具链后重试。"
    elif isinstance(error, ContractError):
        message = "分析请求不符合契约，请检查参数与本轮构建回执。"
    else:
        message = "分析未完成，请保留错误码用于排查；可继续阅读当前源码与知识文档。"
    return f"{message}\n错误码：{error.code}"
