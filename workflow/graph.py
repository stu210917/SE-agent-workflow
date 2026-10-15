"""流程定义：节点 + 带条件的边。

为什么要显式定义「边」而不是把跳转写死在代码里
--------------------------------------------
把 ``if 有问题: goto 修复`` 写进节点内部，流程就散落在各个 Agent 里了：
想知道「这个流程一共几步」得读完所有节点，想改流程得改 Agent。
本项目把拓扑抽出来变成数据，于是：

* 流程可以被**校验**（悬空边、不可达节点、没有出口）；
* 流程可以被**渲染**（Mermaid 流程图 / 终端 ASCII），文档不会和代码脱节；
* 新增 Agent = 加一个节点 + 连两条边，**不用碰引擎**（这正是可扩展性的考点）。

条件分支与循环都靠边上的 ``condition`` 表达：引擎按 ``priority`` 从小到大
依次判断出边，走第一条成立的；没有边成立就结束流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .errors import GraphError
from .node import Node
from .state import WorkflowState

#: 虚拟起点与终点，用于在流程图里画出「从哪开始、到哪结束」
START = "__start__"
END = "__end__"

#: 边条件：给定当前共享状态，判断这条边是否可走
Condition = Callable[[WorkflowState], bool]


@dataclass
class Edge:
    """一条有向边。``condition`` 为 None 表示无条件。"""

    source: str
    target: str
    condition: Condition | None = None
    label: str = ""
    priority: int = 0

    def matches(self, state: WorkflowState) -> bool:
        if self.condition is None:
            return True
        return bool(self.condition(state))

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "conditional": self.condition is not None,
            "priority": self.priority,
        }

    def __str__(self) -> str:
        arrow = "─▶" if self.condition is None else "⇢"
        return f"{self.source} {arrow} {self.target}" + (f"  «{self.label}»" if self.label else "")


class Workflow:
    """一个可执行的流程定义。"""

    def __init__(self, name: str, *, description: str = "", entry: str | None = None) -> None:
        self.name = name
        self.description = description
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._entry = entry

    # ------------------------------------------------------------------ #
    # 组装
    # ------------------------------------------------------------------ #
    def add(self, *nodes: Node) -> "Workflow":
        """注册一个或多个节点（可链式调用）。"""
        for node in nodes:
            if node.name in self._nodes:
                raise GraphError(f"节点重名：{node.name}")
            self._nodes[node.name] = node
        return self

    def connect(
        self,
        source: str,
        target: str,
        *,
        when: Condition | None = None,
        label: str = "",
        priority: int = 0,
    ) -> Edge:
        """连接两个节点（``source`` 可用 :data:`START`）。"""
        edge = Edge(source=source, target=target, condition=when, label=label, priority=priority)
        self._edges.append(edge)
        return edge

    def branch(self, source: str, cases: Sequence[tuple[str, Condition | None, str]]) -> list[Edge]:
        """一次性声明多条出边，优先级按给定顺序。

        :param cases: ``[(目标节点, 条件或 None, 标签), ...]``
        """
        edges = []
        for index, (target, when, label) in enumerate(cases):
            edges.append(self.connect(source, target, when=when, label=label, priority=index))
        return edges

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def entry(self) -> str:
        """入口节点：显式指定，或默认第一个被注册的节点。"""
        if self._entry:
            return self._entry
        if not self._nodes:
            raise GraphError("流程里没有任何节点")
        return next(iter(self._nodes))

    def node(self, name: str) -> Node:
        try:
            return self._nodes[name]
        except KeyError as exc:
            raise GraphError(f"流程里没有节点 `{name}`；已有：{sorted(self._nodes)}") from exc

    def outgoing(self, name: str) -> list[Edge]:
        """按优先级返回某个节点的出边（引擎据此决定下一步）。"""
        edges = [edge for edge in self._edges if edge.source == name]
        return sorted(edges, key=lambda edge: edge.priority)

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        return list(self._edges)

    def node_names(self) -> list[str]:
        return list(self._nodes)

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """硬性检查：不通过则流程根本不该运行。"""
        if not self._nodes:
            raise GraphError("流程里没有任何节点")
        if self._entry and self._entry not in self._nodes:
            raise GraphError(f"入口节点 `{self._entry}` 不在流程里")
        if not self.outgoing(START):
            raise GraphError(f"流程缺少起点：需要至少一条 `{START}` 出发的边")

        for edge in self._edges:
            if edge.source == END:
                raise GraphError(f"`{END}` 是终点，不能有出边（{edge}）")
            if edge.target == START:
                raise GraphError(f"`{START}` 是起点，不能有入边（{edge}）")
            if edge.source != START and edge.source not in self._nodes:
                raise GraphError(f"边的起点 `{edge.source}` 不存在（{edge}）")
            if edge.target != END and edge.target not in self._nodes:
                raise GraphError(f"边的终点 `{edge.target}` 不存在（{edge}）")

        for name, node in self._nodes.items():
            if node.on_error and node.on_error not in self._nodes and node.on_error != END:
                raise GraphError(f"节点 `{name}` 的 on_error 指向不存在的 `{node.on_error}`")

        # 注：节点声明的 reads 无法静态校验——初始状态由引擎在运行时注入
        # （例如 `spec`、`workspace`），静态检查只做「边的两端必须存在」这类
        # 一定能判定的问题，避免把合法流程误判为错误。

    def warnings(self) -> list[str]:
        """软性问题：能跑，但多半不是你想要的。"""
        warnings: list[str] = []
        reachable = self.reachable()
        for name in self._nodes:
            if name not in reachable:
                warnings.append(f"节点 `{name}` 从入口不可达")
        for name, node in self._nodes.items():
            if not self.outgoing(name) and not node.on_error:
                warnings.append(f"节点 `{name}` 没有出边，流程会在此结束（如果不是有意的，请补一条到 `{END}` 的边）")
        return warnings

    def reachable(self) -> set[str]:
        """从 :data:`START` 出发能到达的节点集合。"""
        seen: set[str] = set()
        stack = [edge.target for edge in self.outgoing(START)]
        while stack:
            name = stack.pop()
            if name in seen or name in (START, END) or name not in self._nodes:
                continue
            seen.add(name)
            stack.extend(edge.target for edge in self.outgoing(name))
            stack.extend([self._nodes[name].on_error] if self._nodes[name].on_error else [])
        return seen

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def to_mermaid(self, *, direction: str = "TD") -> str:
        """导出 Mermaid 流程图（可直接贴进 Markdown，GitHub 会渲染）。"""
        lines = [f"flowchart {direction}"]
        lines.append(f'    {START}(["开始"])')
        lines.append(f'    {END}(["结束"])')

        for node in self._nodes.values():
            label = _escape(node.name)
            role = _escape(node.description or node.kind)
            lines.append(f'    {node.name}["<b>{label}</b><br/>{role}"]')

        for edge in sorted(self._edges, key=lambda e: (e.source, e.priority)):
            label = f'|"{_escape(edge.label)}"|' if edge.label else ""
            lines.append(f"    {edge.source} -->{label} {edge.target}")

        # 给条件边加虚线样式，一眼区分「必走」和「视情况走」
        conditional = [edge for edge in self._edges if edge.condition is not None]
        if conditional:
            indexes = ", ".join(
                str(i) for i, edge in enumerate(sorted(self._edges, key=lambda e: (e.source, e.priority)))
                if edge.condition is not None
            )
            lines.append(f"    linkStyle {indexes} stroke-dasharray: 5 5")
        return "\n".join(lines)

    def to_ascii(self) -> str:
        """终端里的紧凑视图。"""
        lines = [f"流程：{self.name}  （入口 {self.entry}，共 {len(self._nodes)} 个节点）"]
        if self.description:
            lines.append(f"      {self.description}")
        lines.append("")
        for node in self._nodes.values():
            marks = []
            if node.reads:
                marks.append("读 " + "/".join(node.reads))
            if node.writes:
                marks.append("写 " + "/".join(node.writes))
            if node.retries:
                marks.append(f"重试 {node.retries}")
            if node.on_error:
                marks.append(f"出错→{node.on_error}")
            suffix = f"   [{'; '.join(marks)}]" if marks else ""
            lines.append(f"  ● {node.name:<12} {node.kind:<8} {node.description}{suffix}")
            for edge in self.outgoing(node.name):
                label = f"  «{edge.label}»" if edge.label else ""
                lines.append(f"      └─▶ {edge.target}{label}")
        for edge in self.outgoing(START):
            lines.insert(3, f"  起点 ─▶ {edge.target}")
        return "\n".join(lines)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "entry": self.entry,
            "nodes": [node.describe() for node in self._nodes.values()],
            "edges": [edge.describe() for edge in self._edges],
            "warnings": self.warnings(),
        }

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workflow {self.name} nodes={len(self._nodes)} edges={len(self._edges)}>"


def _escape(text: str) -> str:
    """Mermaid 标签里不能出现裸引号与尖括号。"""
    return text.replace('"', "'").replace("<", "‹").replace(">", "›")


__all__ = ["Workflow", "Edge", "Condition", "START", "END"]
