"""节点抽象：Workflow 的最小执行单元。

三种节点
--------
* :class:`FunctionNode` —— 包一个普通函数，用来写「保存文件」「打印摘要」这类胶水步骤；
* :class:`AgentNode` —— 包一个 Agent（真正的智能体），节点只负责调度，智能在 Agent 里；
* :class:`ParallelNode` —— 并发跑若干子节点再汇合，演示「并行执行」这一高级流程控制。

**一个刻意的设计约束**：节点不直接写共享状态，而是把产物放进
:class:`NodeResult.artifacts`，由引擎统一写入。这样引擎才能准确地记录
「谁在第几步写了哪个键、是第几个版本」，审计日志不会因为某个节点偷懒而失真。
节点读取状态则不受限制（``ctx.state``）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .errors import NodeError
from .message import Message, MessageBus
from .state import WorkflowState


@dataclass
class NodeResult:
    """节点执行结果。"""

    status: str = "ok"  # ok / skipped
    summary: str = ""
    #: 要写回共享状态的产物：``{键: 值}``
    artifacts: dict[str, Any] = field(default_factory=dict)
    #: 附带的诊断信息（写进事件日志，不进状态）
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, summary: str = "", **artifacts: Any) -> "NodeResult":
        return cls(status="ok", summary=summary, artifacts=dict(artifacts))

    @classmethod
    def skipped(cls, summary: str = "") -> "NodeResult":
        return cls(status="skipped", summary=summary)


class NodeContext:
    """节点执行上下文：把节点需要的一切显式注入，而不是靠全局变量。

    这是「可扩展性」的关键：新增一个 Agent 时，只要它会用 ``ctx``，
    引擎、状态、消息总线都不需要任何改动。
    """

    def __init__(
        self,
        *,
        state: WorkflowState,
        bus: MessageBus,
        step: int,
        node_name: str,
        config: Any = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.state = state
        self.bus = bus
        self.step = step
        self.node_name = node_name
        self.config = config
        self._log = log
        #: 节点自己产生的日志行（会挂到事件上，便于排查）
        self.logs: list[str] = []

    # ------------------------------------------------------------------ #
    def send(self, recipient: str, topic: str, **payload: Any) -> Message:
        """向另一个 Agent 发消息（引擎会自动为它生成一条日志事件）。"""
        return self.bus.send(self.node_name, recipient, topic, payload)

    def broadcast(self, topic: str, **payload: Any) -> Message:
        return self.bus.send(self.node_name, "*", topic, payload)

    def inbox(self, *, since: int = 0) -> list[Message]:
        """读取发给自己的消息（含广播）。"""
        return self.bus.inbox(self.node_name, since=since)

    def log(self, message: str) -> None:
        """记录一行人类可读的说明。"""
        self.logs.append(message)
        if self._log is not None:
            self._log(message)


class Node(ABC):
    """所有节点的基类。"""

    kind = "node"

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        reads: Sequence[str] = (),
        writes: Sequence[str] = (),
        retries: int = 0,
        on_error: str | None = None,
    ) -> None:
        if not name or name in ("__start__", "__end__"):
            raise NodeError(f"非法的节点名：{name!r}（不能为空，也不能用保留名）")
        self.name = name
        self.description = description
        #: 声明式依赖：既用于生成文档，也用于校验流程连线是否合理
        self.reads = tuple(reads)
        self.writes = tuple(writes)
        #: 失败后最多重试几次（针对 NodeRetryableError 与普通异常）
        self.retries = int(retries)
        #: 重试仍失败时转去的节点名（错误恢复路径）
        self.on_error = on_error

    @abstractmethod
    def run(self, ctx: NodeContext) -> NodeResult:
        """执行节点逻辑。"""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "reads": list(self.reads),
            "writes": list(self.writes),
            "retries": self.retries,
            "on_error": self.on_error,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"


class FunctionNode(Node):
    """把普通函数包装成节点（胶水步骤）。"""

    kind = "function"

    def __init__(self, name: str, func: Callable[[NodeContext], Any], **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.func = func

    def run(self, ctx: NodeContext) -> NodeResult:
        result = self.func(ctx)
        if isinstance(result, NodeResult):
            return result
        if result is None:
            return NodeResult.ok(summary=f"{self.name} 执行完成")
        if isinstance(result, dict):
            return NodeResult.ok(summary=f"{self.name} 执行完成", **result)
        return NodeResult.ok(summary=str(result))


class AgentNode(Node):
    """把一个 Agent 接入流程。

    Agent 只需实现 ``act(ctx) -> NodeResult``；节点负责把 Agent 的元信息
    （角色、产出）暴露给流程图与文档。
    """

    kind = "agent"

    def __init__(self, agent: Any, **kwargs: Any) -> None:
        kwargs.setdefault("name", getattr(agent, "name", "") or type(agent).__name__.lower())
        kwargs.setdefault("description", getattr(agent, "role", "") or "")
        kwargs.setdefault("reads", getattr(agent, "reads", ()))
        kwargs.setdefault("writes", getattr(agent, "writes", ()))
        super().__init__(**kwargs)
        self.agent = agent

    def run(self, ctx: NodeContext) -> NodeResult:
        result = self.agent.act(ctx)
        if not isinstance(result, NodeResult):
            raise NodeError(f"Agent {self.name} 的 act() 必须返回 NodeResult，实际是 {type(result).__name__}")
        return result

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["agent"] = type(self.agent).__name__
        return info


class ParallelNode(Node):
    """并发执行若干子节点，再汇合产物（演示「并行执行」）。

    语义刻意保持简单，避免为演示而演示：

    * 子节点之间**不保证顺序**，因此它们的 ``writes`` 不允许重叠，
      否则会抛 :class:`NodeError`——并行写同一个键必然产生竞态；
    * 任一子节点失败则整体失败（引擎按该节点的重试策略处理）；
    * 汇总的 ``summary`` 会列出每个子节点的结果。
    """

    kind = "parallel"

    def __init__(self, name: str, children: Sequence[Node], *, max_workers: int | None = None, **kwargs: Any) -> None:
        if len(children) < 2:
            raise NodeError(f"ParallelNode {name} 至少需要 2 个子节点")
        keys = [key for child in children for key in child.writes]
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        if duplicated:
            raise NodeError(f"ParallelNode {name} 的子节点写出了重复的状态键：{duplicated}（并行写同一键会产生竞态）")
        super().__init__(name, **kwargs)
        self.children = list(children)
        self.max_workers = max_workers or len(self.children)

    def run(self, ctx: NodeContext) -> NodeResult:
        artifacts: dict[str, Any] = {}
        summaries: list[str] = []

        def invoke(child: Node) -> tuple[str, NodeResult]:
            child_ctx = NodeContext(
                state=ctx.state, bus=ctx.bus, step=ctx.step,
                node_name=child.name, config=ctx.config,
            )
            return child.name, child.run(child_ctx)

        # 子节点抛出的任何异常都要包成 NodeError：并行场景下不知道是哪个子节点炸的，
        # 会让上层日志失去排查线索（线程池默认只把原始异常抛回来）
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(invoke, child): child for child in self.children}
            for future, child in futures.items():
                try:
                    child_name, result = future.result()
                except Exception as exc:  # noqa: BLE001 - 统一转换
                    raise NodeError(f"并行子节点 `{child.name}` 执行失败：{type(exc).__name__}: {exc}") from exc
                if result.status != "ok":
                    raise NodeError(f"并行子节点 `{child_name}` 未成功：{result.status} {result.summary}")
                artifacts.update(result.artifacts)
                summaries.append(f"{child_name}: {result.summary}")

        return NodeResult(
            summary="并行完成 → " + "；".join(summaries),
            artifacts=artifacts,
            detail={"children": [child.name for child in self.children]},
        )

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["children"] = [child.describe() for child in self.children]
        return info


__all__ = ["Node", "NodeResult", "NodeContext", "FunctionNode", "AgentNode", "ParallelNode"]
