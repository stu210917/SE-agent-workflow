# -*- coding: utf-8 -*-
"""端到端测试：「编码 → 审查 → 修复 → 复审」整条流程。

这是作业「流程完整性 40%」的直接验收——不测单个零件，而是完整跑一遍，
断言「流程真的跑通了、Agent 真的协作了、问题真的被修掉了」。
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from flows.review_loop import build_review_workflow, initial_state, stop_decision
from workflow import AgentRuntime, EngineConfig, WorkflowEngine, WorkflowState
from workflow.agents import CoderAgent, FixerAgent, ReviewerAgent
from workflow.agents.repairs import FIXERS
from workflow.report import render_run_report

SPEC = "写一个记录导出小工具：读取 CSV、按条件过滤、导出 JSON，并能统计条数。"


class FlowTestCase(unittest.TestCase):
    """公共脚手架：临时工作区 + 离线运行时。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.runtime = AgentRuntime.create(workspace=self.workspace, mock=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_flow(self, *, max_rounds: int = 3, target_score: int = 85, max_steps: int = 40):
        workflow = build_review_workflow(self.runtime, max_rounds=max_rounds, target_score=target_score)
        engine = WorkflowEngine(workflow, config=EngineConfig(max_steps=max_steps), node_config=self.runtime)
        run = engine.run(initial_state(SPEC, self.workspace, max_rounds=max_rounds, target_score=target_score))
        return workflow, run


class EndToEndTests(FlowTestCase):
    def test_flow_completes_and_visits_all_three_agents(self) -> None:
        _, run = self.run_flow()
        visits = run.node_visits()
        self.assertIn("coder", visits)
        self.assertIn("reviewer", visits)
        self.assertIn("fixer", visits, "流程应当至少进入一次修复环节")
        self.assertTrue(run.ok, run.reason)

    def test_generated_code_is_written_and_parseable(self) -> None:
        _, run = self.run_flow()
        code_path = run.state.get("code_path")
        self.assertTrue(code_path)
        target = self.workspace / code_path
        self.assertTrue(target.is_file())
        ast.parse(target.read_text(encoding="utf-8"))  # 最终产物必须语法正确

    def test_quality_improves_across_rounds(self) -> None:
        """这是整个项目最有说服力的一条断言：修复真的让代码变好了。"""
        _, run = self.run_flow()
        history = run.state.get("review_history") or []
        self.assertGreaterEqual(len(history), 2, "至少应有初稿与复审两轮记录")
        first, last = history[0], history[-1]
        self.assertGreater(last["score"], first["score"], f"评分没有提升：{history}")
        self.assertLess(last["total"], first["total"], f"问题数没有下降：{history}")

    def test_loop_runs_at_most_max_rounds(self) -> None:
        _, run = self.run_flow(max_rounds=2)
        reviews = run.node_visits().get("reviewer", 0)
        self.assertLessEqual(reviews, 2, "审查次数不应超过 max_rounds")

    def test_fix_budget_forces_multiple_rounds(self) -> None:
        """每轮改动上限：让「多轮循环逐步收敛」这件事可被真实验证。"""
        workflow = build_review_workflow(self.runtime, max_rounds=6)
        engine = WorkflowEngine(workflow, config=EngineConfig(max_steps=40), node_config=self.runtime)
        run = engine.run(initial_state(SPEC, self.workspace, max_rounds=6, fix_budget=3))

        self.assertGreaterEqual(run.node_visits().get("fixer", 0), 2, "应当修复不止一轮")
        self.assertGreaterEqual(len(run.state.get("review_history") or []), 3, "应当审查不止两轮")
        for log in run.state.versions("fix_log"):
            if log and not log.get("rolled_back"):
                self.assertLessEqual(log.get("applied", 0), 3, f"单轮改动超过上限：{log}")

    def test_budgeted_run_still_converges_to_same_quality(self) -> None:
        """分多轮改完，最终质量应当与一次改完基本一致（否则说明修复丢步了）。"""
        _, one_shot = self.run_flow()
        workflow = build_review_workflow(self.runtime, max_rounds=8)
        engine = WorkflowEngine(workflow, config=EngineConfig(max_steps=40), node_config=self.runtime)
        budgeted = engine.run(initial_state(SPEC, self.workspace, max_rounds=8, fix_budget=2))

        self.assertGreaterEqual(
            budgeted.state.get("review")["score"], one_shot.state.get("review")["score"]
        )

    def test_final_reason_is_explained(self) -> None:
        _, run = self.run_flow()
        self.assertTrue(run.reason.strip())
        self.assertNotIn("没有可走的边", run.reason, "流程应当由条件边主动结束，而不是走投无路")

    def test_respects_step_limit(self) -> None:
        _, run = self.run_flow(max_steps=2)
        self.assertEqual(run.steps, 2)
        self.assertFalse(run.ok)


class CollaborationTests(FlowTestCase):
    """作业「Agent 协作 30%」的验收：消息传递 + 状态共享都要真的发生。"""

    def test_agents_exchange_messages(self) -> None:
        _, run = self.run_flow()
        pairs = {(m.sender, m.recipient) for m in run.bus}
        self.assertIn(("coder", "reviewer"), pairs)
        self.assertIn(("reviewer", "fixer"), pairs)
        self.assertIn(("fixer", "reviewer"), pairs)

    def test_message_payload_carries_counts(self) -> None:
        _, run = self.run_flow()
        review_messages = run.bus.between("reviewer", "fixer")
        self.assertTrue(review_messages)
        payload = review_messages[0].payload
        self.assertIn("total", payload)
        self.assertIn("score", payload)

    def test_state_is_shared_and_versioned(self) -> None:
        _, run = self.run_flow()
        state = run.state
        # 编码 Agent 写初稿、修复 Agent 写改后版本 —— 同名键必须有多个版本
        self.assertGreaterEqual(len(state.versions("code")), 2)
        authors = {change.by for change in state.changes_of_key("code")}
        self.assertEqual(authors, {"coder", "fixer"})
        self.assertIn("findings", state)
        self.assertIn("review", state)

    def test_every_agent_leaves_an_audit_trail(self) -> None:
        """黑板的记账要能回答「谁改了什么」——这是状态管理是否合理的直接体现。"""
        _, run = self.run_flow()
        authors = {change.by for change in run.state.changes()}
        self.assertTrue({"coder", "reviewer", "fixer"} <= authors, f"缺少审计记录：{authors}")

    def test_each_agent_declares_reads_and_writes(self) -> None:
        for agent in (CoderAgent(self.runtime), ReviewerAgent(self.runtime), FixerAgent(self.runtime)):
            self.assertTrue(agent.reads, f"{agent.name} 未声明 reads")
            self.assertTrue(agent.writes, f"{agent.name} 未声明 writes")
            self.assertTrue(agent.role, f"{agent.name} 未声明 role")

    def test_manual_items_are_reported_not_silently_dropped(self) -> None:
        _, run = self.run_flow()
        manual = run.state.get("manual_items") or []
        self.assertTrue(manual, "应当有无法自动修复的问题被明确列出")
        for item in manual:
            self.assertTrue(item.get("reason"), "每一项都要说明为什么不能自动修")


class ReuseTests(FlowTestCase):
    """作业「从项目1复用」的验收。"""

    def test_reviewer_wraps_project_one_review_agent(self) -> None:
        from codereview.agent import ReviewAgent

        reviewer = ReviewerAgent(self.runtime)
        # 审查节点内部就是项目1 的 ReviewAgent，没有另写一套审查逻辑
        self.assertIsInstance(reviewer._review_agent, ReviewAgent)

    def test_findings_come_from_project_one_rules(self) -> None:
        """审查结论必须来自项目1 的规则名——这是「复用」最直接的证据。

        注意要看**历次**审查的并集：修好之后问题就不在了，只看最后一轮会误判。
        状态里保留了 ``findings`` 的每个版本，正好用得上。
        """
        _, run = self.run_flow()
        all_rules = {
            item.get("rule")
            for version in run.state.versions("findings")
            for item in (version or [])
        }
        self.assertTrue(
            all_rules & {"bare-except", "mutable-default", "none-comparison", "unused-import"},
            f"未看到项目1 的规则命中：{sorted(all_rules)}",
        )

    def test_fix_log_references_rule_names(self) -> None:
        _, run = self.run_flow()
        rules = {
            item.get("rule")
            for log in run.state.versions("fix_log")
            for item in (log or {}).get("items", [])
        }
        self.assertTrue(rules & set(FIXERS), f"修复日志里没有已知规则：{sorted(rules)}")


class StopDecisionTests(unittest.TestCase):
    """条件边的判断逻辑：每种终止原因都要能被触发，且给出人能看懂的理由。"""

    def base_state(self, **overrides) -> WorkflowState:
        state = WorkflowState({
            "round": 1, "max_rounds": 3, "target_score": 85,
            "review": {"score": 40, "counts": {"critical": 1, "major": 2, "minor": 0, "info": 0}},
            "findings": [{"rule": "bare-except"}],
        })
        for key, value in overrides.items():
            state.set(key, value, by="test")
        return state

    def test_continues_when_problems_remain(self) -> None:
        stop, reason = stop_decision(self.base_state())
        self.assertFalse(stop)
        self.assertIn("待修复", reason)

    def test_stops_when_no_serious_problems(self) -> None:
        state = self.base_state(review={"score": 70, "counts": {"critical": 0, "major": 0, "minor": 3, "info": 2}})
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("已无严重/重要问题", reason)

    def test_stops_when_target_score_reached(self) -> None:
        state = self.base_state(review={"score": 90, "counts": {"critical": 0, "major": 1, "minor": 0, "info": 0}})
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("目标", reason)

    def test_stops_when_rounds_exhausted(self) -> None:
        state = self.base_state(round=3)
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("最大轮次", reason)

    def test_stops_when_nothing_is_auto_fixable(self) -> None:
        state = self.base_state(findings=[{"rule": "high-complexity"}, {"rule": "eval-exec"}])
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("自动修复范围", reason)

    def test_stops_when_previous_fix_changed_nothing(self) -> None:
        state = self.base_state(fix_log={"applied": 0, "skipped": 3})
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("没有产生任何改动", reason)

    def test_stops_when_fix_was_rolled_back(self) -> None:
        state = self.base_state(fix_log={"applied": 0, "rolled_back": True, "reason": "第 12 行 invalid syntax"})
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("回滚", reason)

    def test_stops_on_degraded_review(self) -> None:
        state = self.base_state(review={"score": 0, "counts": {}, "degraded": True, "degraded_reason": "步数耗尽"})
        stop, reason = stop_decision(state)
        self.assertTrue(stop)
        self.assertIn("降级", reason)


class ReportTests(FlowTestCase):
    def test_report_contains_key_sections(self) -> None:
        workflow, run = self.run_flow()
        report = render_run_report(run, workflow, runtime=self.runtime)
        for section in ("# Workflow 运行报告", "## 一、结论", "## 二、流程图", "## 三、评分演化",
                        "## 四、修复明细", "## 五、Agent 协作记录", "```mermaid", "附录 A"):
            self.assertIn(section, report, f"报告缺少：{section}")

    def test_report_is_regenerable_without_code(self) -> None:
        workflow, run = self.run_flow()
        report = render_run_report(run, workflow, runtime=self.runtime, include_code=False, include_events=False)
        self.assertNotIn("## 六、最终代码", report)
        self.assertNotIn("附录 A", report)

    def test_run_result_is_json_serializable(self) -> None:
        _, run = self.run_flow()
        payload = json.dumps(run.to_dict(), ensure_ascii=False, default=str)
        self.assertIn("code-review-loop", payload)


if __name__ == "__main__":
    unittest.main()
