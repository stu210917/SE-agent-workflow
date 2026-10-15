"""执行日志与状态追踪：流程里发生的每一件事都变成一条结构化事件。

为什么要单独做事件层
--------------------
「执行日志与状态追踪」是作业的推荐实现项，也是 Workflow 类项目最难演示的部分：
流程跑完之后，用户最想知道的是**它到底经历了什么**。所以引擎不打印任何东西，
只负责抛出事件；CLI、Markdown 报告、测试各自决定怎么消费——这与项目1 里
``AgentEvent`` 的设计一脉相承。

事件种类
--------
==========================  ================================================
``kind``                     含义
==========================  ================================================
``workflow_start``           流程开始（附入口节点与节点总数）
``node_start``               节点开始执行
``node_end``                 节点执行成功（附耗时与产物摘要）
``node_retry``               节点失败后重试
``node_error``               节点最终失败
``edge_taken``               走了一条边（附条件标签，用于解释「为什么走这里」）
``message``                  Agent 之间发了一条消息
``state_change``             共享状态被写入
``loop_guard``               触发循环保护（步数上限）
``workflow_end``             流程结束（附终止原因与统计）
==========================  ================================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence


def now() -> str:
    """统一的时间戳格式（秒级精度，日志里够用且不啰嗦）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class WorkflowEvent:
    """流程执行过程中的一条事件。"""

    kind: str
    step: int = 0
    node: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=now)

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"[{self.step:02d}] {self.kind:<15} {self.node:<12} {self.data}"

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at, "step": self.step, "kind": self.kind, "node": self.node, "data": self.data}


#: 事件监听器：接收一条事件，返回 None
Listener = Callable[[WorkflowEvent], None]


class EventRecorder:
    """收集事件，供流程结束后生成日志 / 报告。

    同时支持挂载任意多个监听器（CLI 用它做实时输出，测试用它做断言）。
    """

    def __init__(self, listeners: Sequence[Listener] | None = None) -> None:
        self.events: list[WorkflowEvent] = []
        self._listeners: list[Listener] = list(listeners or [])

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def emit(self, kind: str, *, step: int = 0, node: str = "", **data: Any) -> WorkflowEvent:
        event = WorkflowEvent(kind=kind, step=step, node=node, data=data)
        self.events.append(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # pragma: no cover - 展示层的异常不该影响流程
                pass
        return event

    # ------------------------------------------------------------------ #
    def of_kind(self, *kinds: str) -> list[WorkflowEvent]:
        return [event for event in self.events if event.kind in kinds]

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for event in self.events:
            result[event.kind] = result.get(event.kind, 0) + 1
        return result

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(event.to_dict(), ensure_ascii=False) for event in self.events)

    def __len__(self) -> int:
        return len(self.events)


def render_console(event: WorkflowEvent, *, style: Any = None) -> str:
    """把一条事件渲染成终端里的一行（CLI 默认监听器）。

    ``style`` 只需提供 ``bold`` / ``dim`` / ``green`` / ``yellow`` / ``red`` 等
    可调用属性；传 None 时退化为不加颜色。
    """

    def paint(name: str, text: str) -> str:
        painter = getattr(style, name, None) if style is not None else None
        return painter(text) if callable(painter) else text

    tag = f"{event.step:02d}"
    kind, data = event.kind, event.data

    if kind == "workflow_start":
        return paint("bold", f"▶ 流程启动：{data.get('workflow')}  入口={data.get('entry')}  节点数={data.get('nodes')}")
    if kind == "node_start":
        return paint("cyan", f"[{tag}] ▶ {event.node:<10} {data.get('description', '')}")
    if kind == "node_end":
        duration = f"({data.get('duration_ms', 0)}ms)"
        return (f"[{tag}] {paint('green', '✓')} {event.node:<10} "
                f"{data.get('summary', '')}  {paint('dim', duration)}")
    if kind == "edge_taken":
        label = data.get("label") or "（无条件）"
        return paint("dim", f"        └─ 走边 {data.get('source')} → {data.get('target')}  「{label}」")
    if kind == "message":
        return paint("magenta", f"        ✉ {data.get('sender')} → {data.get('recipient')} "
                                f"[{data.get('topic')}] {data.get('summary', '')}")
    if kind == "state_change":
        return paint("dim", f"        ▤ state[{data.get('key')}] ← {data.get('by')}  {data.get('size', '')}")
    if kind == "node_retry":
        return paint("yellow", f"[{tag}] ⟳ {event.node} 第 {data.get('attempt')} 次重试：{data.get('error', '')[:80]}")
    if kind == "node_error":
        return paint("red", f"[{tag}] ✗ {event.node} 失败：{data.get('error', '')[:120]}")
    if kind == "loop_guard":
        return paint("yellow", f"[{tag}] ⚠ 触发循环保护：{data.get('reason')}")
    if kind == "workflow_end":
        return paint("bold", f"■ 流程结束：{data.get('reason')}  共 {data.get('steps')} 步，"
                             f"{data.get('duration_s')}s")
    return paint("dim", f"[{tag}] {kind} {data}")  # pragma: no cover - 兜底


__all__ = ["WorkflowEvent", "EventRecorder", "Listener", "render_console", "now"]
