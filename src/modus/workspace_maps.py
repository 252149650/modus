"""已观察工作区成员的纯导航投影。

只渲染传入的观察，不发现仓库、不读取成员文件、不写地图。展示刷新不能
扩大对成员仓库的权限；目录分组只用于导航，不建立业务或服务边界。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from .display_labels import display_label
from .paths import WORKSPACE_GENERATED_MAP_NAMES

MAP_NAMES: Final = WORKSPACE_GENERATED_MAP_NAMES
_MAP_TITLES: Final = dict(
    zip(
        MAP_NAMES,
        (
            "成员地图",
            "系统地图",
            "能力地图",
            "服务地图",
            "功能地图",
            "契约地图",
            "对象地图",
            "基础设施地图",
            "交付地图",
            "证据状态",
        ),
        strict=True,
    )
)


# 父目录只控制显示分组，不推导领域、服务或团队所有权。
def member_group(relative_path: str) -> str:
    """Return a display-only parent grouping without creating a 工作区成员."""
    parts = Path(relative_path).parts
    return parts[0] if len(parts) > 1 else "root"


# 相对链接基于生成地图的固定位置，主定义仍留在成员仓库中。
def _map_link(path: str, label: str) -> str:
    return f"[{label}](../../../../../{path})"


def _member_card_link(item: Mapping[str, Any]) -> str:
    return f"[{item['id']}](members/{item['repositoryId']}.md)"


def _append_grouped(rows: dict[str, list[str]], group: str, value: str) -> None:
    rows.setdefault(group, []).append(value)


# 排序让相同观察生成稳定地图；组名反引号需去除以保持标题结构。
def _grouped_rows(rows: Mapping[str, list[str]], *, empty: str) -> list[str]:
    if not rows:
        return [empty]
    rendered: list[str] = []
    for group in sorted(rows):
        safe_group = group.replace("`", "")
        rendered.extend([f"## `{safe_group}`", "", *rows[group], ""])
    return rendered


# 可用、部分可用与缺失知识分别说明，不把导航存在等同于事实完整。
# 成员卡只引用已有入口，不复制成员正文成为第二份主定义。
def member_markdown(item: Mapping[str, Any]) -> str:
    entries = item.get("knowledge_entries", [])
    knowledge_state = str((item.get("knowledge") or {}).get("state") or "missing")
    if knowledge_state == "available" and entries:
        knowledge_summary = "成员知识可用，请结合当前源码阅读以下入口。"
    elif knowledge_state == "partial":
        knowledge_summary = "成员知识尚不完整，请以当前源码核验结论。"
    else:
        knowledge_summary = "暂无正式成员知识，请直接核查当前源码。"
    return "\n".join(
        [
            f"# 成员： {item['id']}",
            "",
            f"- 路径： `{item['path']}`",
            f"- 导航分组： `{member_group(str(item['path']))}`",
            f"- 领域： {', '.join(item.get('domains', [])) or '暂无领域文档'}",
            "",
            "## 可用知识",
            "",
            knowledge_summary,
            "",
            *[f"- [{entry}](../../../../../../{entry})" for entry in entries],
            "",
        ]
    )


# 路由依据规范文件用途，目录命名不被解释为业务关系。
# 同一文档可支持多个地图，但链接仍指向原始主定义。
def _maps_for_knowledge_entry(entry: str) -> tuple[str, ...]:
    """Route one canonical knowledge filename to the maps it can inform."""

    name = Path(entry).name
    if name == "architecture-overview.md":
        return "system-map", "delivery-map"
    if name == "SKILL.md":
        return ("capability-map",)
    if "/references/modules/" in entry:
        return ("feature-map",)
    if name in {"api.md", "downstream.md"}:
        return "service-map", "contract-map"
    if name == "object.md":
        return ("object-map",)
    if name == "infrastructure.md":
        return "infrastructure-map", "delivery-map"
    return ()


def _append_member_map_rows(
    rows: dict[str, dict[str, list[str]]],
    item: Mapping[str, Any],
) -> None:
    """Project one live member into every applicable navigation map."""

    group = member_group(str(item["path"]))
    member = _member_card_link(item)

    def add(name: str, text: str) -> None:
        _append_grouped(rows[name], group, f"- {member} — {text}")

    add("member-map", f"路径 `{item['path']}`；仓库 `{item['repositoryId']}`")
    state = item.get("knowledge", {})
    add(
        "fact-status",
        f"知识状态 `{display_label(str(state.get('state', 'missing')))}`； {state.get('reason', '')}",
    )
    add(
        "system-map",
        "源码与知识归属此成员；请结合部署与调用证据核验系统边界。",
    )
    for entry in item.get("knowledge_entries", []):
        for map_name in _maps_for_knowledge_entry(entry):
            add(map_name, _map_link(entry, entry))
    for source in item.get("sourceEntries", []):
        add("system-map", _map_link(source, source))


# 所有声明地图都输出，缺少材料时给出明确空状态。
# 渲染只消费传入观察，不在展示期间再次读取成员仓库。
# 地图数量和标题由共享路径契约决定，保持安装与刷新一致。
def render_maps(members: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Project current member documents and source paths into readable navigation."""
    rows: dict[str, dict[str, list[str]]] = {name: {} for name in MAP_NAMES}
    for item in members:
        _append_member_map_rows(rows, item)
    return {
        name: "\n".join(
            [
                f"# {_MAP_TITLES[name]}",
                "",
                "父目录仅用于导航分组。判断能力或连接关系前，请核查当前成员知识与源码。",
                "",
                *_grouped_rows(
                    rows[name],
                    empty="暂无可读的知识入口，请核查成员源码。",
                ),
                "",
            ]
        )
        for name in MAP_NAMES
    }


__all__ = ["MAP_NAMES", "member_group", "member_markdown", "render_maps"]
