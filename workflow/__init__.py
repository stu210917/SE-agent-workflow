"""agent-workflow —— 基于「编码 → 审查 → 修复」的多 Agent 协作 Workflow。

    from workflow import AgentRuntime, WorkflowEngine, build_review_workflow

    runtime = AgentRuntime.create(workspace="./runs/demo")
    workflow = build_review_workflow(runtime, max_rounds=3)
    run = WorkflowEngine(workflow, node_config=runtime).run({"spec": "...", "workspace": ...})
    print(run.summary_line())

模块地图
--------
====================  ==========================================================
模块                  职责
====================  ==========================================================
``state``             共享状态（黑板）：产物、版本、审计记录
``message``           Agent 间消息总线（定向 + 广播 + 全量存档）
``node``              节点抽象（函数节点 / Agent 节点 / 并行节点）
``graph``             流程定义（节点 + 条件边）、校验与 Mermaid 导出
``engine``            执行引擎：顺序、条件分支、循环、重试、错误恢复
``events``            执行日志与状态追踪
``agents``            三个 Agent：编码 / 审查（复用项目1）/ 修复
====================  ==========================================================
"""

from __future__ import annotations

from .agents import Agent, AgentRuntime, CoderAgent, FixerAgent, ReviewerAgent
from .engine import EngineConfig, WorkflowEngine, WorkflowRun
from .errors import (
    AgentError,
    GraphError,
    NodeError,
    NodeRetryableError,
    StateError,
    WorkflowError,
)
from .events import EventRecorder, WorkflowEvent, render_console
from .graph import END, START, Edge, Workflow
from .message import Message, MessageBus
from .node import AgentNode, FunctionNode, Node, NodeContext, NodeResult, ParallelNode
from .state import WorkflowState

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # 运行时与 Agent
    "Agent",
    "AgentRuntime",
    "CoderAgent",
    "ReviewerAgent",
    "FixerAgent",
    # 引擎
    "WorkflowEngine",
    "WorkflowRun",
    "EngineConfig",
    # 流程定义
    "Workflow",
    "Edge",
    "START",
    "END",
    # 状态与消息
    "WorkflowState",
    "MessageBus",
    "Message",
    # 节点
    "Node",
    "NodeContext",
    "NodeResult",
    "AgentNode",
    "FunctionNode",
    "ParallelNode",
    # 事件
    "WorkflowEvent",
    "EventRecorder",
    "render_console",
    # 异常
    "WorkflowError",
    "GraphError",
    "NodeError",
    "NodeRetryableError",
    "StateError",
    "AgentError",
]
