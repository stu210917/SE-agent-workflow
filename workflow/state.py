"""共享状态（黑板模式）。

**为什么用黑板而不是让 Agent 互相传参？**
Workflow 里节点是解耦的：编码 Agent 不该知道审查 Agent 的类名，更不该知道
「修复 Agent 需要几个字段」。黑板把所有产物摊在一个带命名空间的字典里，
节点只声明「我读哪些键、我写哪些键」，于是：

* 新增一个 Agent 不需要改动已有 Agent；
* 换掉某个节点的实现（比如把规则修复换成 LLM 修复）不影响其他人；
* 每次写入都留痕，可以回答「这个值是谁、在第几步写下的」。

**版本化**：同名键被反复写入时（例如 ``code`` 会被编码和修复各写一次），
历史值不会丢，``state.versions("code")`` 可以取出完整演化链——这正是
「审查 → 修复 → 复审」这类循环流程必须的能力。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .errors import StateKeyError, StateTypeError
from .events import now


@dataclass(frozen=True)
class StateChange:
    """一次状态写入的记录（审计用）。"""

    step: int
    key: str
    by: str
    note: str
    version: int
    size: int
    at: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step, "key": self.key, "by": self.by,
            "note": self.note, "version": self.version, "size": self.size, "at": self.at,
        }

    def __str__(self) -> str:
        return f"step {self.step:>2}  {self.by:<10} 写 state[{self.key}]  v{self.version}  {self.note}"


class WorkflowState:
    """全流程共享的黑板。"""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})
        self._history: dict[str, list[Any]] = {key: [value] for key, value in self._data.items()}
        self._changes: list[StateChange] = []
        self.step = 0

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def require(self, key: str, expected_type: type | tuple[type, ...] | None = None) -> Any:
        """读取一个**必须存在**的键；可选校验类型。

        Agent 缺输入时应当立刻报错并给出清晰提示，而不是拿到 ``None``
        再在十行之后抛 ``AttributeError``。
        """
        if key not in self._data:
            raise StateKeyError(
                f"状态里没有 `{key}`；当前已有的键：{sorted(self._data)}"
            )
        value = self._data[key]
        if expected_type is not None and not isinstance(value, expected_type):
            name = getattr(expected_type, "__name__", str(expected_type))
            raise StateTypeError(f"state[{key}] 期望 {name}，实际是 {type(value).__name__}")
        return value

    def has(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> list[str]:
        return sorted(self._data)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def set(self, key: str, value: Any, *, by: str = "?", note: str = "") -> StateChange:
        """写入一个键，并登记版本与审计记录。"""
        self._data[key] = value
        versions = self._history.setdefault(key, [])
        versions.append(value)
        change = StateChange(
            step=self.step, key=key, by=by, note=note,
            version=len(versions), size=_describe(value),
        )
        self._changes.append(change)
        return change

    def update(self, values: Mapping[str, Any], *, by: str = "?", note: str = "") -> list[StateChange]:
        return [self.set(key, value, by=by, note=note) for key, value in values.items()]

    def increment(self, key: str, amount: int = 1, *, by: str = "?") -> int:
        """自增计数（例如轮次 ``round``）——循环流程里最常用的一种状态。"""
        value = int(self._data.get(key, 0)) + amount
        self.set(key, value, by=by, note=f"+{amount}")
        return value

    # ------------------------------------------------------------------ #
    # 版本与审计
    # ------------------------------------------------------------------ #
    def versions(self, key: str) -> list[Any]:
        """取某个键的全部历史值（按写入顺序）。"""
        return list(self._history.get(key, []))

    def previous(self, key: str, back: int = 1) -> Any:
        """取 ``back`` 个版本之前的值（用于对比「修复前 vs 修复后」）。"""
        versions = self._history.get(key, [])
        index = len(versions) - 1 - back
        if index < 0:
            raise StateKeyError(f"state[{key}] 没有第 {back} 个历史版本（共 {len(versions)} 个）")
        return versions[index]

    def changes(self) -> list[StateChange]:
        return list(self._changes)

    def changes_by(self, author: str) -> list[StateChange]:
        """按**写入者**筛选（例如「修复 Agent 改过哪些状态」）。

        注意参数是节点名而不是状态键——想按键筛选请用 :meth:`changes_of_key`，
        这两个概念很容易混，所以刻意拆成两个名字明确的方法。
        """
        return [change for change in self._changes if change.by == author]

    def changes_of_key(self, key: str) -> list[StateChange]:
        """按**状态键**筛选（例如「``code`` 这个产物被谁改过几次」）。"""
        return [change for change in self._changes if change.key == key]

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def snapshot(self, *, max_text: int = 200) -> dict[str, Any]:
        """生成可序列化的快照：长文本只留摘要，避免日志文件爆炸。"""
        result: dict[str, Any] = {}
        for key, value in self._data.items():
            if isinstance(value, str) and len(value) > max_text:
                result[key] = f"<str {len(value)} 字符> {value[:max_text]}…"
            elif isinstance(value, (int, float, bool)) or value is None:
                result[key] = value
            elif isinstance(value, (list, tuple)):
                result[key] = f"<{type(value).__name__} 长度 {len(value)}>"
            elif isinstance(value, dict):
                result[key] = {k: v for k, v in list(value.items())[:max_text]}
            else:
                result[key] = f"<{type(value).__name__}>"
        return result

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=indent)

    def render_changes(self, limit: int = 30) -> str:
        lines = [str(change) for change in self._changes[-limit:]]
        return "\n".join(lines) if lines else "（本次流程没有产生任何状态变更）"

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)


def _describe(value: Any) -> int:
    """用字符数粗略描述值的体积，日志里比「<object>」有用得多。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return 1


__all__ = ["WorkflowState", "StateChange"]
