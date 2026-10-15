"""Agent 基类与共享运行时。

**复用项目1 的方式**：本项目的审查能力直接来自项目1 的 ``codereview`` 包
（以 vendored 方式内置，保证项目2 可独立运行）。三个 Agent 共享同一个
:class:`AgentRuntime`，它持有项目1 的 :class:`~codereview.config.Config` 与
LLM 客户端——因此：

* 环境变量、``.env``、离线 Mock 降级等配置机制**与项目1 完全一致**，
  用户在项目1 里配好的 ``CR_API_KEY`` 在本项目里直接生效；
* 没有 API Key 时自动进入 Mock 模式：编码 Agent 用内置模板产出朴素初稿，
  修复 Agent 用确定性规则修复，审查 Agent 用 AST 引擎——**整条流程离线可跑**。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..node import NodeContext, NodeResult


@dataclass
class AgentRuntime:
    """三个 Agent 共用的运行时依赖。"""

    config: Any  # codereview.config.Config
    llm: Any  # codereview.llm.BaseLLM
    workspace: Path

    # ------------------------------------------------------------------ #
    @property
    def mock(self) -> bool:
        """是否处于离线 Mock 模式（没有可用 API Key）。"""
        return bool(self.config.effective_mock)

    @property
    def mode_label(self) -> str:
        return "离线 Mock" if self.mock else f"真实 LLM（{self.config.model}）"

    @classmethod
    def create(cls, workspace: str | Path | None = None, **overrides: Any) -> "AgentRuntime":
        """按项目1 的配置优先级（CLI > 环境变量 > .env > 默认）构建运行时。"""
        from codereview.config import Config
        from codereview.llm import create_llm

        root = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
        root.mkdir(parents=True, exist_ok=True)
        clean = {key: value for key, value in overrides.items() if value is not None}
        config = Config.from_env(workspace=root, **clean)
        return cls(config=config, llm=create_llm(config), workspace=root)

    # ------------------------------------------------------------------ #
    def complete(self, system: str, user: str, *, temperature: float | None = None) -> str:
        """调用一次 LLM 并返回正文（供编码 / 修复 Agent 在真实模式下使用）。"""
        response = self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
        )
        return response.content

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode_label,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "workspace": str(self.workspace),
        }


class Agent(ABC):
    """参与 Workflow 的智能体。

    每个 Agent 只声明三件事：**我是谁**（``name`` / ``role``）、
    **我读什么**（``reads``）、**我写什么**（``writes``）——然后实现 ``act``。
    流程编排、状态记账、消息投递全部由引擎负责，Agent 之间互不引用。
    """

    #: 节点名（在流程图与日志里显示）
    name: str = ""
    #: 角色说明（画进流程图）
    role: str = ""
    #: 声明读取的状态键（文档与校验用）
    reads: tuple[str, ...] = ()
    #: 声明写入的状态键
    writes: tuple[str, ...] = ()

    def __init__(self, runtime: AgentRuntime) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} 必须定义 name")
        self.runtime = runtime

    @abstractmethod
    def act(self, ctx: NodeContext) -> NodeResult:
        """执行本 Agent 的职责，返回产物。"""

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "class": type(self).__name__,
            "reads": list(self.reads),
            "writes": list(self.writes),
            "mode": self.runtime.mode_label,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"


def append_history(ctx: NodeContext, key: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """把一条记录追加到状态里的历史列表（节点只能整体写回产物，所以要读-改-写）。"""
    history = list(ctx.state.get(key) or [])
    history.append(entry)
    return history


__all__ = ["Agent", "AgentRuntime", "append_history"]
