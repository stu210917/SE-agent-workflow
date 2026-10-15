"""内置流程集合。

引擎（``workflow/``）与具体流程（``flows/``）刻意分开：
引擎只认识「节点 + 条件边」，不认识「编码」「审查」这些业务概念。
因此新增一条流程 = 在本目录新建一个文件 + 组装节点，**引擎一行都不用改**。
"""

from __future__ import annotations

from .metrics_flow import build_metrics_workflow
from .review_loop import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_TARGET_SCORE,
    build_review_workflow,
    initial_state,
    stop_decision,
    stop_reason,
)

#: 可用流程名 → 构建函数（CLI 的 --flow 参数据此解析）
FLOWS = {
    "review-loop": build_review_workflow,
    "metrics-parallel": build_metrics_workflow,
}

__all__ = [
    "build_review_workflow",
    "build_metrics_workflow",
    "initial_state",
    "stop_decision",
    "stop_reason",
    "FLOWS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_TARGET_SCORE",
]
