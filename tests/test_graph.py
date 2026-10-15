# -*- coding: utf-8 -*-
"""流程定义（节点 / 条件边 / 校验 / 渲染）测试。"""

from __future__ import annotations

import unittest

from workflow import MessageBus, WorkflowState
from workflow.errors import GraphError, NodeError
from workflow.graph import END, START, Workflow
from workflow.node import FunctionNode, NodeContext, NodeResult, ParallelNode


def const_node(name: str, **artifacts):
    """生成一个「总是成功并写入固定产物」的节点。"""
    return FunctionNode(
        name,
        lambda ctx: NodeResult.ok(summary=f"{name} 完成", **artifacts),
        writes=tuple(artifacts),
    )


class WorkflowDefinitionTests(unittest.TestCase):
    def test_entry_defaults_to_first_added(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"), const_node("b"))
        self.assertEqual(workflow.entry, "a")

    def test_explicit_entry(self) -> None:
        workflow = Workflow("t", entry="b")
        workflow.add(const_node("a"), const_node("b"))
        self.assertEqual(workflow.entry, "b")

    def test_duplicate_node_name_rejected(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"))
        with self.assertRaises(GraphError):
            workflow.add(const_node("a"))

    def test_reserved_names_rejected(self) -> None:
        for bad in ("", START, END):
            with self.assertRaises(NodeError):
                const_node(bad)

    def test_connect_and_outgoing_order(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"), const_node("b"), const_node("c"))
        workflow.connect("a", "c", priority=1, label="第二条")
        workflow.connect("a", "b", priority=0, label="第一条")
        self.assertEqual([edge.target for edge in workflow.outgoing("a")], ["b", "c"])

    def test_branch_helper_assigns_priority_in_order(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"), const_node("b"), const_node("c"))
        edges = workflow.branch("a", [("b", None, "先"), ("c", None, "后")])
        self.assertEqual([edge.priority for edge in edges], [0, 1])
        self.assertEqual([edge.label for edge in edges], ["先", "后"])


class WorkflowValidationTests(unittest.TestCase):
    def test_missing_entry_edge_is_error(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"))
        with self.assertRaises(GraphError) as ctx:
            workflow.validate()
        self.assertIn("起点", str(ctx.exception))

    def test_dangling_edge_is_error(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", "ghost")
        with self.assertRaises(GraphError) as ctx:
            workflow.validate()
        self.assertIn("ghost", str(ctx.exception))

    def test_end_cannot_have_outgoing_edge(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"))
        workflow.connect(START, "a")
        workflow.connect(END, "a")
        with self.assertRaises(GraphError):
            workflow.validate()

    def test_start_cannot_have_incoming_edge(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", START)
        with self.assertRaises(GraphError):
            workflow.validate()

    def test_on_error_target_must_exist(self) -> None:
        workflow = Workflow("t")
        workflow.add(FunctionNode("a", lambda ctx: NodeResult.ok(), on_error="nope"))
        workflow.connect(START, "a")
        with self.assertRaises(GraphError):
            workflow.validate()

    def test_valid_flow_passes(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"), const_node("b"))
        workflow.connect(START, "a")
        workflow.connect("a", "b")
        workflow.connect("b", END)
        workflow.validate()

    def test_unreachable_node_produces_warning_not_error(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"), const_node("orphan"))
        workflow.connect(START, "a")
        workflow.connect("a", END)
        workflow.validate()  # 不报错
        self.assertTrue(any("orphan" in w for w in workflow.warnings()))
        self.assertNotIn("orphan", workflow.reachable())

    def test_node_without_outgoing_edge_is_warned(self) -> None:
        workflow = Workflow("t")
        workflow.add(const_node("a"))
        workflow.connect(START, "a")
        self.assertTrue(any("没有出边" in w for w in workflow.warnings()))


class EdgeConditionTests(unittest.TestCase):
    def test_unconditional_edge_always_matches(self) -> None:
        workflow = Workflow("t")
        edge = workflow.connect("a", "b")
        self.assertTrue(edge.matches(WorkflowState()))

    def test_condition_receives_current_state(self) -> None:
        from workflow import WorkflowState

        workflow = Workflow("t")
        edge = workflow.connect("a", "b", when=lambda state: state.get("go") is True)
        self.assertFalse(edge.matches(WorkflowState({"go": False})))
        self.assertTrue(edge.matches(WorkflowState({"go": True})))


class RenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = Workflow("演示流程", description="用于测试")
        self.workflow.add(
            FunctionNode("coder", lambda ctx: NodeResult.ok(), description="编码", writes=("code",)),
            FunctionNode("reviewer", lambda ctx: NodeResult.ok(), description="审查", reads=("code",)),
        )
        self.workflow.connect(START, "coder")
        self.workflow.branch("coder", [("reviewer", None, "无条件")])
        self.workflow.branch("reviewer", [
            (END, lambda state: True, "达标"),
            ("coder", None, "返工"),
        ])

    def test_mermaid_contains_nodes_and_conditional_edges(self) -> None:
        mermaid = self.workflow.to_mermaid()
        self.assertTrue(mermaid.startswith("flowchart TD"))
        self.assertIn('coder["<b>coder</b><br/>编码"]', mermaid)
        self.assertIn('reviewer -->|"达标"| __end__', mermaid)
        self.assertIn("linkStyle", mermaid)  # 条件边用虚线区分

    def test_mermaid_escapes_quotes(self) -> None:
        workflow = Workflow("t")
        workflow.add(FunctionNode('we"ird', lambda ctx: NodeResult.ok(), description='带"引号"'))
        workflow.connect(START, 'we"ird')
        self.assertNotIn('"带"引号""', workflow.to_mermaid())

    def test_ascii_renders_declarations(self) -> None:
        text = self.workflow.to_ascii()
        self.assertIn("读 code", text)
        self.assertIn("写 code", text)
        self.assertIn("起点 ─▶ coder", text)

    def test_describe_is_serializable(self) -> None:
        import json

        json.dumps(self.workflow.describe(), ensure_ascii=False)


class ParallelNodeTests(unittest.TestCase):
    def test_runs_children_and_merges_artifacts(self) -> None:
        parallel = ParallelNode("fan", [const_node("a", x=1), const_node("b", y=2)])
        ctx = NodeContext(state=WorkflowState(), bus=MessageBus(),
                          step=1, node_name="fan")
        result = parallel.run(ctx)
        self.assertEqual(result.artifacts, {"x": 1, "y": 2})
        self.assertIn("a", result.summary)
        self.assertIn("b", result.summary)

    def test_overlapping_writes_are_rejected(self) -> None:
        with self.assertRaises(NodeError) as ctx:
            ParallelNode("fan", [const_node("a", same=1), const_node("b", same=2)])
        self.assertIn("竞态", str(ctx.exception))

    def test_needs_at_least_two_children(self) -> None:
        with self.assertRaises(NodeError):
            ParallelNode("fan", [const_node("a")])

    def test_child_failure_fails_the_parallel_node(self) -> None:
        def boom(ctx: NodeContext) -> NodeResult:
            raise RuntimeError("子节点炸了")

        parallel = ParallelNode("fan", [const_node("ok", a=1), FunctionNode("bad", boom)])
        ctx = NodeContext(state=WorkflowState(), bus=MessageBus(),
                          step=1, node_name="fan")
        with self.assertRaises(NodeError):
            parallel.run(ctx)


if __name__ == "__main__":
    unittest.main()
