"""Select representative capability seeds deterministically."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .composition_model import Seed


# 预算只在超限时改变选择，未裁剪输入保留调用者原顺序。
# 目录轮询防止单个大目录耗尽全部调查名额，同时保留触发类型优先级。
def select_seeds(
    seeds: tuple[Seed, ...],
    entries: Mapping[str, Mapping[str, Any]],
    *,
    max_slices: int,
) -> tuple[Seed, ...]:
    """Prefer explicit triggers while sampling source areas deterministically.

    Within a trigger rank, round-robin directories before taking another seed
    from the same directory. Stable path, line and identity ordering makes the
    budgeted sample reproducible. An untruncated input retains its original order.
    """

    if len(seeds) <= max_slices:
        return seeds
    by_rank_and_directory: defaultdict[int, defaultdict[str, list[Seed]]] = defaultdict(
        lambda: defaultdict(list)
    )
    trigger_rank = {
        "external-http": 0,
        "external-rpc": 0,
        "mq": 0,
        "job": 0,
        "cli": 0,
        "declared-entry": 1,
        "public-export": 2,
    }
    for seed in seeds:
        entry = entries.get(seed.entry_id, {})
        location = entry.get("location") or {}
        path = str(location.get("path") or entry.get("sourceFile") or "")
        directory = str(Path(path).parent) if path else "."
        rank = trigger_rank.get(str(seed.trigger.get("kind") or ""), 1)
        by_rank_and_directory[rank][directory].append(seed)

    selected: list[Seed] = []
    for rank in sorted(by_rank_and_directory):
        queues = {
            directory: deque(
                sorted(
                    values,
                    key=lambda seed: (
                        str(
                            (entries.get(seed.entry_id, {}).get("location") or {}).get("path") or ""
                        ),
                        int(
                            (entries.get(seed.entry_id, {}).get("location") or {}).get("startLine")
                            or 0
                        ),
                        seed.signature,
                        seed.entry_id,
                    ),
                )
            )
            for directory, values in sorted(by_rank_and_directory[rank].items())
        }
        while queues and len(selected) < max_slices:
            for directory in tuple(queues):
                selected.append(queues[directory].popleft())
                if not queues[directory]:
                    del queues[directory]
                if len(selected) == max_slices:
                    break
        if len(selected) == max_slices:
            break
    return tuple(selected)


__all__ = ["select_seeds"]
