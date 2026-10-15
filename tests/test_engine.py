# -*- coding: utf-8 -*-
"""执行引擎测试：顺序、条件分支、循环、重试、错误恢复、并行、循环保护。

这些是作业「必须实现」与「推荐实现」两条要求的直接验收：
顺序执行是底线，分支与循环是本项目真正想演示的能力。
"""

from __future__ import annotations

import unittest

from workflow import EngineConfig, EventRecorder, WorkflowEngine
from workflow.errors import NodeRetryableError
from workflow.graph import END, START, Workflow
from workflow.node import FunctionNode, NodeContext, NodeResult


def node(name: str, *, retries: int = 0, on_error: str | None = None, writes: tuple[str, ...] = ()):
    """生成一个把执行次数记进状态的节点。"""
    def run(ctx: NodeContext) -> NodeResult:
        count = int(ctx.state.get(f"{name}_runs") or 0) + 1
        return NodeResult.ok(summary=f"{name} 第 {count} 次", **{f"{name}_runs": count})

    return FunctionNode(name, run, retries=retries, on_error=on_error, writes=writes or (f"{name}_runs",))


class SequentialTests(unittest.TestCase):
    def test_runs_nodes_in_order(self) -> None:
        workflow = Workflow("seq")
        workflow.add(node("a"), node("b"), node("c"))
        workflow.connect(START, "a")
        workflow.connect("a", "b")
        workflow.connect("b", "c")
        workflow.connect("c", END)

        run = WorkflowEngine(workflow).run()
        self.assertEqual(run.visited, ["a", "b", "c"])
        self.assertEqual(run.steps, 3)
        self.assertTrue(run.ok)
        self.assertEqual(run.state.get("c_runs"), 1)

    def test_artifacts_are_written_with_author(self) -> None:
        workflow = Workflow("seq")
        workflow.add(node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", END)

        run = WorkflowEngine(workflow).run()
        change = run.state.changes()[0]
        self.assertEqual(change.by, "a")
        self.assertEqual(change.key, "a_runs")

    def test_stops_at_node_without_outgoing_edge(self) -> None:
        workflow = Workflow("seq")
        workflow.add(node("a"))
        workflow.connect(START, "a")
        run = WorkflowEngine(workflow).run()
        self.assertIn("没有可走的边", run.reason)


class BranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = Workflow("branch")
        self.workflow.add(node("start"), node("yes"), node("no"))
        self.workflow.connect(START, "start")
        self.workflow.branch("start", [
            ("yes", lambda state: state.get("pick") == "yes", "选 yes"),
            ("no", lambda state: state.get("pick") == "no", "选 no"),
        ])

    def test_first_matching_edge_wins(self) -> None:
        run = WorkflowEngine(self.workflow).run({"pick": "yes"})
        self.assertIn("yes", run.visited)
        self.assertNotIn("no", run.visited)

    def test_second_edge_taken_when_first_fails(self) -> None:
        run = WorkflowEngine(self.workflow).run({"pick": "no"})
        self.assertIn("no", run.visited)

    def test_no_matching_edge_ends_flow(self) -> None:
        run = WorkflowEngine(self.workflow).run({"pick": "other"})
        self.assertEqual(run.visited, ["start"])
        self.assertIn("没有可走的边", run.reason)

    def test_edge_taken_event_records_label(self) -> None:
        run = WorkflowEngine(self.workflow).run({"pick": "yes"})
        labels = [event.data.get("label") for event in run.recorder.of_kind("edge_taken")]
        self.assertIn("选 yes", labels)


class LoopTests(unittest.TestCase):
    """循环是「审查 → 修复」流程的核心，必须能跑也能停。"""

    def test_loops_until_condition_met(self) -> None:
        def counting_worker(ctx: NodeContext) -> NodeResult:
            return NodeResult.ok(summary="干活", round=int(ctx.state.get("round") or 0) + 1)

        workflow = Workflow("loop")
        workflow.add(FunctionNode("worker", counting_worker, writes=("round",)), node("gate"))
        workflow.connect(START, "worker")
        workflow.connect("worker", "gate")
        workflow.branch("gate", [
            (END, lambda state: int(state.get("round") or 0) >= 3, "轮次用尽"),
            ("worker", None, "继续"),
        ])

        run = WorkflowEngine(workflow).run()
        self.assertEqual(run.node_visits()["worker"], 3)
        self.assertEqual(run.state.get("round"), 3)
        self.assertIn("轮次用尽", run.reason)

    def test_max_steps_guard_stops_runaway_loop(self) -> None:
        """两个节点互相指向时必须被兜住，否则演示现场会挂死。"""
        workflow = Workflow("infinite")
        workflow.add(node("ping"), node("pong"))
        workflow.connect(START, "ping")
        workflow.connect("ping", "pong")
        workflow.connect("pong", "ping")

        run = WorkflowEngine(workflow, config=EngineConfig(max_steps=7)).run()
        self.assertEqual(run.steps, 7)
        self.assertFalse(run.ok)
        self.assertIn("步数上限", run.reason)
        self.assertTrue(run.recorder.of_kind("loop_guard"))

    def test_max_steps_can_be_overridden_per_run(self) -> None:
        workflow = Workflow("infinite")
        workflow.add(node("ping"))
        workflow.connect(START, "ping")
        workflow.connect("ping", "ping")
        run = WorkflowEngine(workflow, config=EngineConfig(max_steps=99)).run(max_steps=4)
        self.assertEqual(run.steps, 4)


class FailureTests(unittest.TestCase):
    def _failing_node(self, name: str, *, fail_times: int, retries: int = 0, on_error=None):
        attempts = {"n": 0}

        def run(ctx: NodeContext) -> NodeResult:
            attempts["n"] += 1
            if attempts["n"] <= fail_times:
                raise NodeRetryableError(f"{name} 第 {attempts['n']} 次失败")
            return NodeResult.ok(summary=f"{name} 成功（第 {attempts['n']} 次）")

        return FunctionNode(name, run, retries=retries, on_error=on_error), attempts

    def test_retry_until_success(self) -> None:
        workflow = Workflow("retry")
        failing, attempts = self._failing_node("flaky", fail_times=2, retries=3)
        workflow.add(failing)
        workflow.connect(START, "flaky")
        workflow.connect("flaky", END)

        run = WorkflowEngine(workflow).run()
        self.assertTrue(run.ok)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(run.recorder.of_kind("node_retry")), 2)

    def test_failure_after_retries_exhausted(self) -> None:
        workflow = Workflow("retry")
        failing, _ = self._failing_node("bad", fail_times=99, retries=2)
        workflow.add(failing)
        workflow.connect(START, "bad")
        workflow.connect("bad", END)

        run = WorkflowEngine(workflow).run()
        self.assertFalse(run.ok)
        self.assertEqual(len(run.failures), 1)
        self.assertEqual(run.failures[0]["attempts"], 3)
        self.assertTrue(run.recorder.of_kind("node_error"))

    def test_on_error_edge_recovers(self) -> None:
        """错误恢复路径：失败后转去兜底节点，而不是让整条流程崩掉。"""
        workflow = Workflow("recover")
        failing, _ = self._failing_node("bad", fail_times=99, on_error="fallback")
        workflow.add(failing, node("fallback"))
        workflow.connect(START, "bad")
        workflow.connect("fallback", END)

        run = WorkflowEngine(workflow).run()
        self.assertIn("fallback", run.visited)
        labels = [event.data.get("label") for event in run.recorder.of_kind("edge_taken")]
        self.assertIn("错误恢复", labels)

    def test_stop_on_failure_can_be_disabled(self) -> None:
        workflow = Workflow("soft")
        failing, _ = self._failing_node("bad", fail_times=99)
        workflow.add(failing, node("after"))
        workflow.connect(START, "bad")
        workflow.connect("bad", "after")
        workflow.connect("after", END)

        engine = WorkflowEngine(workflow, config=EngineConfig(stop_on_failure=False))
        run = engine.run()
        self.assertFalse(run.ok)
        self.assertEqual(run.failures[0]["node"], "bad")


class ObservabilityTests(unittest.TestCase):
    def test_listener_receives_events_in_order(self) -> None:
        seen: list[str] = []
        workflow = Workflow("obs")
        workflow.add(node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", END)

        engine = WorkflowEngine(workflow, listeners=[lambda event: seen.append(event.kind)])
        engine.run()
        self.assertEqual(seen[0], "workflow_start")
        self.assertEqual(seen[-1], "workflow_end")
        self.assertIn("node_start", seen)
        self.assertIn("node_end", seen)

    def test_recorder_counts_and_jsonl(self) -> None:
        recorder = EventRecorder()
        workflow = Workflow("obs")
        workflow.add(node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", END)
        WorkflowEngine(workflow, recorder=recorder).run()

        self.assertGreater(recorder.counts().get("node_end", 0), 0)
        self.assertIn('"kind": "workflow_start"', recorder.to_jsonl())

    def test_message_sent_by_node_becomes_event(self) -> None:
        def sender(ctx: NodeContext) -> NodeResult:
            ctx.send("someone", "hello", summary="打个招呼")
            return NodeResult.ok(summary="已发送")

        workflow = Workflow("msg")
        workflow.add(FunctionNode("talker", sender))
        workflow.connect(START, "talker")
        workflow.connect("talker", END)

        run = WorkflowEngine(workflow).run()
        messages = run.recorder.of_kind("message")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].data["recipient"], "someone")
        self.assertEqual(len(run.bus), 1)

    def test_broken_listener_does_not_break_the_run(self) -> None:
        def bad_listener(event) -> None:
            raise RuntimeError("展示层炸了")

        workflow = Workflow("obs")
        workflow.add(node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", END)
        run = WorkflowEngine(workflow, listeners=[bad_listener]).run()
        self.assertTrue(run.ok)

    def test_run_to_dict_is_serializable(self) -> None:
        import json

        workflow = Workflow("obs")
        workflow.add(node("a"))
        workflow.connect(START, "a")
        workflow.connect("a", END)
        run = WorkflowEngine(workflow).run({"spec": "x"})
        json.dumps(run.to_dict(), ensure_ascii=False, default=str)


class ParallelIntegrationTests(unittest.TestCase):
    def test_parallel_node_inside_workflow(self) -> None:
        from workflow.node import ParallelNode

        workflow = Workflow("par")
        fan = ParallelNode("fan", [node("left"), node("right")])
        workflow.add(fan, node("join"))
        workflow.connect(START, "fan")
        workflow.connect("fan", "join")
        workflow.connect("join", END)

        run = WorkflowEngine(workflow).run()
        self.assertTrue(run.ok)
        self.assertEqual(run.state.get("left_runs"), 1)
        self.assertEqual(run.state.get("right_runs"), 1)


if __name__ == "__main__":
    unittest.main()
