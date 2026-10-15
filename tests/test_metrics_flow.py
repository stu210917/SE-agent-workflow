# -*- coding: utf-8 -*-
"""第二条流程（并行度量）测试。

这条流程存在的意义是证明两件事：**并行执行真的能跑**，以及**新增流程不用改引擎**。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flows import FLOWS
from flows.metrics_flow import build_metrics_workflow
from workflow import EngineConfig, MessageBus, WorkflowEngine
from workflow.agents import AgentRuntime

SAMPLE = '''\
"""示例模块。"""

import os


def simple(a, b):
    """简单函数。"""
    return a + b


def branchy(value, flag=True):
    """分支较多的函数。"""
    if not value:
        return 0
    if flag and value > 10:
        return value * 2
    for index in range(len(value)):
        if index % 2:
            continue
    # TODO: 补一个默认分支
    return value
'''


class MetricsFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.scan_root = self.root / "project"
        (self.scan_root / "pkg").mkdir(parents=True)
        (self.scan_root / "pkg" / "sample.py").write_text(SAMPLE, encoding="utf-8")
        self.workspace = self.root / "run"

        self.runtime = AgentRuntime.create(workspace=self.workspace, mock=True)
        self.workflow = build_metrics_workflow(self.runtime)
        self.engine = WorkflowEngine(
            self.workflow, config=EngineConfig(max_steps=10), node_config=self.runtime
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_flow(self):
        return self.engine.run({"workspace": self.workspace, "scan_root": str(self.scan_root)})

    # ------------------------------------------------------------------ #
    def test_flow_is_registered(self) -> None:
        self.assertIn("metrics-parallel", FLOWS)

    def test_flow_completes(self) -> None:
        run = self.run_flow()
        self.assertTrue(run.ok, run.reason)
        self.assertEqual(run.steps, 3)

    def test_parallel_node_produced_all_three_artifacts(self) -> None:
        """并行节点必须把三路子节点的产物都汇合上来。"""
        run = self.run_flow()
        for key in ("lines_stats", "complexity_stats", "todo_stats"):
            self.assertIn(key, run.state, f"并行节点没有产出 {key}")

    def test_metrics_are_accurate(self) -> None:
        run = self.run_flow()
        lines = run.state.get("lines_stats")
        self.assertGreater(lines["total"], 15)
        self.assertEqual(lines["total"], lines["code"] + lines["comment"] + lines["blank"])

        complexity = run.state.get("complexity_stats")
        self.assertEqual(complexity["functions"], 2)
        self.assertGreaterEqual(complexity["max"], 3, "branchy 有多个分支，复杂度应大于 3")

    def test_todo_scan_finds_marker(self) -> None:
        run = self.run_flow()
        todos = run.state.get("todo_stats")
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["kind"], "TODO")

    def test_report_written_to_disk(self) -> None:
        run = self.run_flow()
        report = self.workspace / "metrics-report.md"
        self.assertTrue(report.is_file())
        text = report.read_text(encoding="utf-8")
        self.assertIn("# 代码度量报告", text)
        self.assertIn("复杂度", text)

    def test_workspace_is_excluded_from_scan(self) -> None:
        """运行产物目录不该被扫进来，否则第二次运行会把上一步的产物也算上。"""
        (self.workspace).mkdir(parents=True, exist_ok=True)
        (self.workspace / "generated.py").write_text("x = 1\n", encoding="utf-8")
        run = self.run_flow()
        self.assertNotIn("generated.py", " ".join(run.state.get("files") or []))

    def test_missing_scan_root_reports_clear_error(self) -> None:
        run = self.engine.run({"workspace": self.workspace, "scan_root": str(self.root / "nope")})
        self.assertFalse(run.ok)
        self.assertIn("不存在", run.failures[0]["error"])

    def test_no_new_messages_are_needed(self) -> None:
        """这条流程不靠消息协作，纯靠状态——正好说明两种协作机制各有所长。"""
        bus = MessageBus()
        engine = WorkflowEngine(self.workflow, bus=bus, node_config=self.runtime)
        engine.run({"workspace": self.workspace, "scan_root": str(self.scan_root)})
        self.assertEqual(len(bus), 0)


if __name__ == "__main__":
    unittest.main()
