"""协议值的中文展示名称；序列化与业务判断始终使用原始值。

展示词汇不依赖 CLI、分析管线或文件系统，终端与生成的 Markdown 可共同使用。
未知值保留原始标识，避免新协议状态被错误描述为已知结果。
"""

from __future__ import annotations

_LABELS = {
    "available": "可用",
    "partial": "不完整",
    "missing": "缺失",
    "unavailable": "不可用",
    "up-to-date": "已是最新",
    "update-available": "有可用更新",
    "framework-newer": "框架版本较新",
    "installed-newer": "已安装版本较新",
    "not-installed": "未安装",
    "unknown": "未知",
    "anchor_metadata_invalid": "锚点元数据无效",
    "symbol_missing": "符号缺失",
    "line_drift": "行号漂移",
    "ambiguous": "匹配不唯一",
    "ref_file_missing": "引用文件缺失",
    "ref_line_out_of_range": "引用行号越界",
}


def display_label(value: str) -> str:
    return _LABELS.get(value, f"诊断（{value}）")
