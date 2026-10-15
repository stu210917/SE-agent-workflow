"""审查 Agent：**直接复用项目1 的审查能力**。

这是「从项目1复用」这条要求的落点：本 Agent 不重写任何审查逻辑，
只是把项目1 的 :class:`codereview.agent.ReviewAgent` 包成一个 Workflow 节点——
读取共享状态里的代码路径，跑完整套 ReAct 审查循环（侦察 → AST 分析 →
精读 → 搜索 → 收敛），把结构化报告写回状态。

复用的好处在这里很直观：项目1 花大力气做的 26 条 AST 规则、路径沙箱、
上下文压缩、降级交付，在本项目里**一行都没重写**就直接生效了；
连离线 Mock 降级、重试退避这些行为也完全一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codereview.agent import ReviewAgent
from codereview.report import Issue
from codereview.tools.base import build_default_registry

from ..node import NodeContext, NodeResult
from .base import Agent, append_history

#: 严重度排序权重（越小越严重）
_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}


class ReviewerAgent(Agent):
    """把项目1 的代码审查 Agent 接入 Workflow。"""

    name = "reviewer"
    role = "审查 Agent：复用项目1 的 AST 规则引擎做静态审查"
    reads = ("code_path", "workspace")
    writes = ("review", "findings", "review_history")

    def __init__(self, runtime: Any) -> None:
        super().__init__(runtime)
        # 审查 Agent 无状态（每次 review() 都会重置证据库），因此可以复用同一实例
        self._review_agent = ReviewAgent(
            self.runtime.config,
            registry=build_default_registry(self.runtime.config),
        )

    # ------------------------------------------------------------------ #
    def act(self, ctx: NodeContext) -> NodeResult:
        rel_path = str(ctx.state.require("code_path", str))
        workspace = Path(ctx.state.require("workspace"))
        target = workspace / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"待审查文件不存在：{target}")

        result = self._review_agent.review([rel_path])
        report = result.report
        findings = [issue.to_dict() for issue in report.issues]
        counts = report.counts

        review = {
            "score": report.score,
            "grade": report.grade,
            "summary": report.summary,
            "counts": counts,
            "issues": findings,
            "degraded": report.degraded,
            "degraded_reason": report.degraded_reason,
            "steps": report.meta.get("steps", 0),
            "tool_calls": report.meta.get("tool_calls", 0),
        }
        round_no = int(ctx.state.get("round") or 1)
        history = append_history(ctx, "review_history", {
            "round": round_no, "score": report.score, "counts": counts,
            "total": len(findings), "by": self.name,
        })

        fixable = sum(1 for item in findings if item.get("rule") in _FIXABLE)
        ctx.send(
            "fixer", "review_done",
            summary=(f"第 {round_no} 轮审查：{len(findings)} 条问题"
                     f"（严重 {counts['critical']} / 重要 {counts['major']}），评分 {report.score}"),
            round=round_no, score=report.score, total=len(findings),
            critical=counts["critical"], major=counts["major"], fixable=fixable,
        )
        ctx.log(f"第 {round_no} 轮审查完成：{len(findings)} 条问题，评分 {report.score}")

        # 把最严重的几条写进 summary，让终端一眼看到重点
        worst = sorted(findings, key=lambda item: (_RANK.get(item.get("severity", "minor"), 9), item.get("line", 0)))[:3]
        detail_titles = "；".join(f"{item['severity']}:{item['title']}" for item in worst)

        return NodeResult(
            summary=(f"第 {round_no} 轮：{len(findings)} 条问题"
                     f"（c{counts['critical']}/M{counts['major']}/m{counts['minor']}/i{counts['info']}），"
                     f"评分 {report.score}"),
            artifacts={
                "review": review,
                "findings": findings,
                "review_history": history,
            },
            detail={"worst": detail_titles, "fixable": fixable},
        )


def _fixable_rules() -> set[str]:
    """延迟导入修复规则表，避免 agents 之间循环引用。"""
    from .repairs import FIXERS

    return set(FIXERS)


_FIXABLE = _fixable_rules()


__all__ = ["ReviewerAgent", "Issue"]
