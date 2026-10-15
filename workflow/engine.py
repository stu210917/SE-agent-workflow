"""执行引擎：把「节点 + 边」跑起来。

一个回合（step）的生命周期
--------------------------
::

    ┌──────────────────────────────────────────────────────────────┐
    │ 1. 选中当前节点（开始时是入口节点）                            │
    │ 2. 发 node_start 事件                                        │
    │ 3. 执行 node.run(ctx)                                        │
    │      失败 → 重试（最多 node.retries 次，指数退避）             │
    │      仍失败 → 走 node.on_error 恢复分支，或终止流程            │
    │ 4. 把 NodeResult.artifacts 写回共享状态（引擎统一记账）         │
    │ 5. 把节点期间发的消息逐条转成事件                              │
    │ 6. 发 node_end 事件                                          │
    │ 7. 按优先级评估出边条件 → 走第一条成立的边（发 edge_taken）      │
    │      没有边成立 / 走到 END → 流程结束                         │
    └──────────────────────────────────────────────────────────────┘

三道保护，避免流程失控
----------------------
1. **步数上限**（``max_steps``）：循环流程必须有人喊停，否则两个互相指向的
   节点可以永远跑下去；
2. **最大轮次由流程自己表达**：本项目的审查循环用状态里的 ``round`` 和
   ``max_rounds`` 控制，条件边负责判断——引擎不硬编码任何业务规则；
3. **失败不静默**：节点最终失败时一定会产生 ``node_error`` 事件，
   并记录到 :class:`WorkflowRun.failures`，报告里能查到。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import NodeRetryableError
from .events import EventRecorder, Listener, WorkflowEvent
from .graph import END, Workflow
from .message import MessageBus
from .node import Node, NodeContext, NodeResult
from .state import WorkflowState


@dataclass
class EngineConfig:
    """引擎行为配置。"""

    #: 单次运行的最大步数（硬性熔断）
    max_steps: int = 50
    #: 节点失败后的退避基数（秒），第 n 次重试等待 backoff_base ** n
    backoff_base: float = 0.0
    #: 节点最终失败时是否终止整个流程（False 则继续评估出边）
    stop_on_failure: bool = True


@dataclass
class WorkflowRun:
    """一次流程运行的完整结果。"""

    workflow: str
    state: WorkflowState
    bus: MessageBus
    recorder: EventRecorder
    steps: int = 0
    reason: str = ""
    duration_s: float = 0.0
    ok: bool = True
    failures: list[dict[str, Any]] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def events(self) -> Sequence[WorkflowEvent]:
        return self.recorder.events

    def artifact(self, key: str, default: Any = None) -> Any:
        """便捷读取最终产物（等价于 ``run.state.get``）。"""
        return self.state.get(key, default)

    def node_visits(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in self.visited:
            counts[name] = counts.get(name, 0) + 1
        return counts

    def summary_line(self) -> str:
        visits = "、".join(f"{name}×{count}" for name, count in self.node_visits().items())
        return (f"{self.workflow}：{self.reason}；共 {self.steps} 步，{self.duration_s:.2f}s；"
                f"节点执行 {visits or '（无）'}；消息 {len(self.bus)} 条")

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "ok": self.ok,
            "reason": self.reason,
            "steps": self.steps,
            "duration_s": round(self.duration_s, 3),
            "visited": self.visited,
            "node_visits": self.node_visits(),
            "messages": [message.to_dict() for message in self.bus],
            "state": self.state.snapshot(),
            "state_changes": [change.to_dict() for change in self.state.changes()],
            "failures": self.failures,
        }


class WorkflowEngine:
    """流程执行引擎。"""

    def __init__(
        self,
        workflow: Workflow,
        *,
        config: EngineConfig | None = None,
        bus: MessageBus | None = None,
        recorder: EventRecorder | None = None,
        node_config: Any = None,
        listeners: Sequence[Listener] | None = None,
    ) -> None:
        workflow.validate()
        self.workflow = workflow
        self.config = config or EngineConfig()
        self.bus = bus if bus is not None else MessageBus()
        self.recorder = recorder if recorder is not None else EventRecorder(listeners)
        #: 传给节点的业务配置（例如 LLM 设置），引擎自己不用
        self.node_config = node_config
        self._listeners = list(listeners or [])

    # ------------------------------------------------------------------ #
    def run(self, initial_state: dict[str, Any] | None = None, *, max_steps: int | None = None) -> WorkflowRun:
        """执行流程。

        :param initial_state: 注入黑板的初始键（如 ``spec``、``workspace``）。
        :param max_steps: 覆盖配置里的步数上限（测试与演示常用）。
        """
        started = time.perf_counter()
        state = WorkflowState(initial_state)
        run = WorkflowRun(
            workflow=self.workflow.name, state=state, bus=self.bus,
            recorder=self.recorder, reason="未开始",
        )
        limit = int(max_steps or self.config.max_steps)

        self.recorder.emit(
            "workflow_start", workflow=self.workflow.name,
            entry=self.workflow.entry, nodes=len(self.workflow),
            description=self.workflow.description,
        )

        current = self.workflow.entry
        reason = "流程走到了没有出边的节点"
        ok = True

        while run.steps < limit:
            if current == END:  # pragma: no cover - 出边解析时已拦截
                reason = "到达终点"
                break

            node = self.workflow.node(current)
            run.steps += 1
            state.step = run.steps
            run.visited.append(node.name)

            result, failure = self._execute(node, state, run)
            if failure is not None:
                ok = False
                if node.on_error:
                    # 错误恢复：走 on_error 分支，而不是直接结束
                    self.recorder.emit("edge_taken", step=run.steps, node=node.name,
                                       source=node.name, target=node.on_error, label="错误恢复")
                    current = node.on_error
                    continue
                reason = f"节点 `{node.name}` 失败：{failure}"
                break

            run.reason = result.summary or ""
            target, label = self._next_node(node, state, run)
            if target is None:
                reason = result.detail.get("terminal_reason") or f"节点 `{node.name}` 之后没有可走的边"
                break
            if target == END:
                # 用「走的是哪条边」作为终止原因：条件边的标签本来就是人写的业务语义
                # （「已达标」「轮次用尽」），比「走到了终点」有用得多
                reason = result.detail.get("terminal_reason") or (
                    f"满足结束条件并退出：{label}" if label else f"从 `{node.name}` 走到终点"
                )
                break
            current = target
        else:
            reason = f"达到步数上限 {limit}（已触发循环保护）"
            self.recorder.emit("loop_guard", step=run.steps, reason=reason, max_steps=limit)
            ok = False

        run.reason = reason
        run.ok = ok
        run.duration_s = time.perf_counter() - started
        self.recorder.emit(
            "workflow_end", step=run.steps, reason=reason, ok=ok,
            steps=run.steps, duration_s=round(run.duration_s, 3),
            messages=len(self.bus), state_keys=len(state),
        )
        return run

    # ------------------------------------------------------------------ #
    # 单节点执行（含重试）
    # ------------------------------------------------------------------ #
    def _execute(
        self,
        node: Node,
        state: WorkflowState,
        run: WorkflowRun,
    ) -> tuple[NodeResult, str | None]:
        """执行节点，返回 ``(结果, 失败原因)``；成功时失败原因为 None。"""
        self.recorder.emit("node_start", step=run.steps, node=node.name, description=node.description)
        started = time.perf_counter()
        attempts = node.retries + 1
        messages_before = len(self.bus)
        last_error = ""

        for attempt in range(1, attempts + 1):
            ctx = NodeContext(
                state=state, bus=self.bus, step=run.steps,
                node_name=node.name, config=self.node_config,
            )
            try:
                result = node.run(ctx)
                self._apply(node, result, state, run, messages_before)
                duration_ms = int((time.perf_counter() - started) * 1000)
                self.recorder.emit(
                    "node_end", step=run.steps, node=node.name, status=result.status,
                    summary=result.summary, duration_ms=duration_ms,
                    artifacts=sorted(result.artifacts), logs=ctx.logs[:5],
                )
                return result, None
            except Exception as exc:  # noqa: BLE001 - 任何异常都必须变成流程事件，不能让引擎崩掉
                last_error = f"{type(exc).__name__}: {exc}"
                # 只有显式声明「可重试」的异常才值得重试；其余照重试策略执行，
                # 但会在事件里标出来，方便排查「为什么重试了却没救回来」
                retryable = isinstance(exc, NodeRetryableError)

            if attempt < attempts:
                delay = self.config.backoff_base ** attempt if self.config.backoff_base else 0.0
                self.recorder.emit("node_retry", step=run.steps, node=node.name,
                                   attempt=attempt, error=last_error, delay=round(delay, 3),
                                   retryable=retryable)
                if delay:
                    time.sleep(delay)

        failure = {"node": node.name, "step": run.steps, "error": last_error, "attempts": attempts}
        run.failures.append(failure)
        self.recorder.emit("node_error", step=run.steps, node=node.name,
                           error=last_error, attempts=attempts)
        return NodeResult(status="failed", summary=f"{node.name} 失败"), last_error

    def _apply(
        self,
        node: Node,
        result: NodeResult,
        state: WorkflowState,
        run: WorkflowRun,
        messages_before: int,
    ) -> None:
        """把产物写回状态，并把节点期间发出的消息转成事件。"""
        for key, value in result.artifacts.items():
            change = state.set(key, value, by=node.name, note=result.summary[:60])
            self.recorder.emit("state_change", step=run.steps, node=node.name,
                               key=key, by=node.name, version=change.version, size=change.size)

        for message in self.bus.history()[messages_before:]:
            self.recorder.emit("message", step=run.steps, node=node.name,
                               sender=message.sender, recipient=message.recipient,
                               topic=message.topic, summary=message.summary())

    def _next_node(self, node: Node, state: WorkflowState, run: WorkflowRun) -> tuple[str | None, str]:
        """按优先级评估出边。

        :return: ``(下一个节点名或 END, 该边的标签)``；没有可用出边时返回 ``(None, "")``。
        """
        for edge in self.workflow.outgoing(node.name):
            if not edge.matches(state):
                continue
            self.recorder.emit(
                "edge_taken", step=run.steps, node=node.name,
                source=edge.source, target=edge.target,
                label=edge.label, conditional=edge.condition is not None,
            )
            return edge.target, edge.label
        return None, ""


__all__ = ["WorkflowEngine", "EngineConfig", "WorkflowRun"]
