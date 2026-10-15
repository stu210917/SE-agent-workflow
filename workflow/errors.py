"""Workflow 引擎的异常体系。

与项目1 的做法一致：把「可恢复」与「不可恢复」的错误显式分开，
引擎才能决定是重试、走错误分支，还是直接终止流程。

    WorkflowError
      ├── GraphError           流程定义本身有问题（悬空边、无入口、重名节点）
      ├── NodeError            节点执行失败
      │     ├── NodeRetryableError   可重试（网络抖动、LLM 限流）
      │     └── NodeAborted          主动放弃（重试次数用尽）
      ├── StateError           状态读写问题（缺键、类型不符）
      └── AgentError           Agent 层错误（LLM 不可用、修复规则缺失）
"""

from __future__ import annotations


class WorkflowError(Exception):
    """本项目所有异常的基类。"""


# --------------------------------------------------------------------------- #
# 流程定义
# --------------------------------------------------------------------------- #
class GraphError(WorkflowError):
    """流程定义非法：例如边指向不存在的节点、没有入口、节点重名。"""


# --------------------------------------------------------------------------- #
# 节点执行
# --------------------------------------------------------------------------- #
class NodeError(WorkflowError):
    """节点执行失败。"""


class NodeRetryableError(NodeError):
    """可重试的节点失败（网络抖动、LLM 限流等）。"""


class NodeAborted(NodeError):
    """重试次数用尽后放弃该节点。"""


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
class StateError(WorkflowError):
    """状态读写问题。"""


class StateKeyError(StateError):
    """读取了不存在的状态键。"""


class StateTypeError(StateError):
    """状态值类型不符合预期。"""


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class AgentError(WorkflowError):
    """Agent 层错误。"""


class AgentInputError(AgentError):
    """Agent 收到的输入不完整（例如修复 Agent 没拿到审查报告）。"""


__all__ = [
    "WorkflowError",
    "GraphError",
    "NodeError",
    "NodeRetryableError",
    "NodeAborted",
    "StateError",
    "StateKeyError",
    "StateTypeError",
    "AgentError",
    "AgentInputError",
]
