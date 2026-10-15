"""Agent 集合：编码 → 审查 → 修复。

每个 Agent 只依赖 :class:`~workflow.agents.base.AgentRuntime` 与 ``NodeContext``，
彼此之间**没有直接引用**——想换掉其中任何一个（比如把规则修复换成 LLM 修复），
只需要新建一个类并在流程定义里换掉节点，其余代码一行都不用动。
"""

from __future__ import annotations

from .base import Agent, AgentRuntime, append_history
from .coder import CoderAgent
from .fixer import FixerAgent
from .repairs import FIXERS, MANUAL_RULES, SourceEditor
from .reviewer import ReviewerAgent

__all__ = [
    "Agent",
    "AgentRuntime",
    "append_history",
    "CoderAgent",
    "ReviewerAgent",
    "FixerAgent",
    "FIXERS",
    "MANUAL_RULES",
    "SourceEditor",
]
