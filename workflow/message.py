"""Agent 之间的消息传递。

作业要求「实现 Agent 间的消息传递**或**状态共享」——本项目两者都做了，
而且分工明确：

* :class:`~workflow.state.WorkflowState`（黑板）负责**产物**：代码、审查报告、修复记录。
  它是「共享内存」，任何节点都能读，适合大块数据。
* :class:`MessageBus`（本模块）负责**意图**：谁让谁做什么、做完了通知谁。
  它是「收件箱」，适合小块的、有指向性的协作信号。

为什么要分开？只做黑板会丢失「协作意图」——事后复盘时只看到数据变了，
却说不清是谁在指挥谁；只做消息则每次都要把大段代码塞进消息里。
两者结合，日志里既有「reviewer 请 fixer 修复 3 个 critical」这样的因果链，
也有可追溯的产物版本。

消息是**不可变**的（``frozen=True``），并且每条都带序号与时间戳，
因此可以安全地存档、回放和断言。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .events import now

#: 广播收件人：所有 Agent 都能收到
BROADCAST = "*"


@dataclass(frozen=True)
class Message:
    """一条 Agent 间消息。"""

    sender: str
    recipient: str
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    at: str = field(default_factory=now)

    @property
    def is_broadcast(self) -> bool:
        return self.recipient == BROADCAST

    def summary(self, limit: int = 90) -> str:
        """一行摘要，用于终端与日志。"""
        if "summary" in self.payload:
            text = str(self.payload["summary"])
        elif self.payload:
            text = json.dumps(self.payload, ensure_ascii=False)
        else:
            text = ""
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "at": self.at, "sender": self.sender,
            "recipient": self.recipient, "topic": self.topic, "payload": self.payload,
        }


class MessageBus:
    """极简消息总线：发送、定向接收、广播、全量存档。

    刻意**不做**异步投递与订阅回调——本项目的节点是顺序执行的，
    异步会让「谁先谁后」变得不可复现。需要并发时由引擎的并行节点负责，
    消息总线依然是先发先存，保证日志可回放。
    """

    def __init__(self) -> None:
        self._messages: list[Message] = []

    # ------------------------------------------------------------------ #
    def send(
        self,
        sender: str,
        recipient: str,
        topic: str,
        payload: dict[str, Any] | None = None,
    ) -> Message:
        """发送一条定向消息。"""
        message = Message(
            sender=sender, recipient=recipient, topic=topic,
            payload=dict(payload or {}), seq=len(self._messages) + 1,
        )
        self._messages.append(message)
        return message

    def broadcast(self, sender: str, topic: str, payload: dict[str, Any] | None = None) -> Message:
        """广播给所有 Agent。"""
        return self.send(sender, BROADCAST, topic, payload)

    # ------------------------------------------------------------------ #
    def inbox(self, agent: str, *, since: int = 0) -> list[Message]:
        """取某个 Agent 收到的全部消息（含广播），``since`` 可用于只看新的。"""
        return [
            message for message in self._messages[since:]
            if message.recipient in (agent, BROADCAST)
        ]

    def history(self) -> list[Message]:
        return list(self._messages)

    def between(self, sender: str, recipient: str) -> list[Message]:
        return [m for m in self._messages if m.sender == sender and m.recipient == recipient]

    def topics(self) -> list[str]:
        seen: list[str] = []
        for message in self._messages:
            if message.topic not in seen:
                seen.append(message.topic)
        return seen

    # ------------------------------------------------------------------ #
    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps([message.to_dict() for message in self._messages], ensure_ascii=False, indent=indent)

    def render(self, limit: int = 20) -> str:
        """渲染成人类可读的消息流水（报告附录用）。"""
        lines = []
        for message in self._messages[-limit:]:
            arrow = "⇢" if message.is_broadcast else "→"
            lines.append(f"{message.seq:>3}. [{message.at}] {message.sender} {arrow} "
                         f"{message.recipient}  «{message.topic}»  {message.summary()}")
        return "\n".join(lines) if lines else "（本次流程没有产生任何消息）"

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)


__all__ = ["Message", "MessageBus", "BROADCAST"]
