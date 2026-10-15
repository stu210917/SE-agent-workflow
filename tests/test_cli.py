# -*- coding: utf-8 -*-
"""命令行界面测试（进程内调用 ``main``，不起子进程）。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from workflow.cli import EXIT_OK, EXIT_USAGE, main

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_cli(argv: Sequence[str]) -> tuple[int, str, str]:
    """执行 CLI，返回 ``(退出码, stdout, stderr)``。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class StaticCommandsTests(unittest.TestCase):
    def test_no_arguments_prints_help(self) -> None:
        code, output, _ = run_cli([])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("usage:", output)
        self.assertIn("编码 → 审查 → 修复", output)

    def test_graph_ascii(self) -> None:
        code, output, _ = run_cli(["graph", "--format", "ascii"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("coder", output)
        self.assertIn("reviewer", output)
        self.assertIn("fixer", output)
        self.assertIn("起点", output)

    def test_graph_mermaid(self) -> None:
        code, output, _ = run_cli(["graph", "--format", "mermaid"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("flowchart TD", output)
        self.assertIn("__end__", output)

    def test_agents_lists_declarations(self) -> None:
        code, output, _ = run_cli(["agents"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("读取状态", output)
        self.assertIn("写入状态", output)
        self.assertIn("CoderAgent", output)
        self.assertIn("ReviewerAgent", output)
        self.assertIn("FixerAgent", output)

    def test_rules_lists_both_groups(self) -> None:
        code, output, _ = run_cli(["rules"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("可自动修复的规则", output)
        self.assertIn("需人工处理的规则", output)
        self.assertIn("none-comparison", output)
        self.assertIn("eval-exec", output)

    def test_unknown_flow_returns_usage_error(self) -> None:
        code, _, err = run_cli(["graph", "--flow", "nope"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("未知流程", err)


class RunCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "run"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_smoke(self) -> None:
        code, output, _ = run_cli([
            "run", "--mock", "--no-color", "--quiet",
            "--workspace", str(self.workspace), "--out", str(self.workspace / "report.md"),
        ])
        self.assertIn(code, (EXIT_OK, 1))
        self.assertIn("流程结果", output)
        self.assertIn("报告已写入", output)
        self.assertTrue((self.workspace / "report.md").is_file())
        self.assertTrue((self.workspace / "solution.py").is_file())

    def test_json_output_is_machine_readable(self) -> None:
        code, output, _ = run_cli([
            "run", "--mock", "--no-color", "--quiet", "--json",
            "--workspace", str(self.workspace),
        ])
        payload = json.loads(output)
        self.assertIn("state", payload)
        self.assertIn("messages", payload)
        self.assertIn("workflow", payload)
        self.assertEqual(payload["workflow"], "code-review-loop")

    def test_custom_spec_is_used(self) -> None:
        code, output, _ = run_cli([
            "run", "--mock", "--no-color", "--quiet",
            "--spec", "写一个配置读取工具", "--workspace", str(self.workspace),
        ])
        self.assertIn(code, (EXIT_OK, 1))
        # 关键词命中「配置读取」模板
        code_text = (self.workspace / "solution.py").read_text(encoding="utf-8")
        self.assertIn("配置", code_text)

    def test_spec_file_is_read(self) -> None:
        spec_file = Path(self._tmp.name) / "spec.md"
        spec_file.write_text("写一个 CSV 导出工具", encoding="utf-8")
        code, _, _ = run_cli([
            "run", "--mock", "--no-color", "--quiet",
            "--spec-file", str(spec_file), "--workspace", str(self.workspace),
        ])
        self.assertIn(code, (EXIT_OK, 1))

    def test_missing_spec_file_returns_usage_error(self) -> None:
        code, _, err = run_cli(["run", "--spec-file", "不存在的文件.md", "--mock"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("不存在", err)

    def test_max_rounds_one_stops_early(self) -> None:
        code, output, _ = run_cli([
            "run", "--mock", "--no-color", "--max-rounds", "1",
            "--workspace", str(self.workspace), "--json",
        ])
        payload = json.loads(output)
        self.assertLessEqual(payload["node_visits"].get("reviewer", 0), 1)


if __name__ == "__main__":
    unittest.main()
