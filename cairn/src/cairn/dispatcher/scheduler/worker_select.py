"""Worker 排序策略，按优先级、当前负载和随机扰动选择候选。"""

from __future__ import annotations

import random

from cairn.dispatcher.config import WorkerConfig


def choose_worker(
    candidates: list[WorkerConfig],
    running_counts: dict[str, int],
    tried_workers: set[str] | None = None,
) -> list[WorkerConfig]:
    # 优先选没试过该 intent 的 worker（换 worker 重试），再按 priority/running_count/随机排。
    tried = tried_workers or set()
    grouped = sorted(
        candidates,
        key=lambda worker: (
            worker.name in tried,  # False(没试过) 排在 True(试过) 前面
            worker.priority,
            running_counts.get(worker.name, 0),
            random.random(),
        ),
    )
    return grouped
