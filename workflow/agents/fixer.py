"""修复 Agent：按审查结论改代码，并**验证改完还能跑**。

三条底线
--------
1. **只改报告点到的地方**：修复规则按 `规则名 + 行号` 定位，不做「顺手重构」，
   因此改动范围可预期、可 diff、可回归；
2. **改完必须能编译**：渲染后的源码先过一遍 ``ast.parse``，语法不过就**整轮丢弃**
   并把失败原因写进日志——宁可这一轮白干，也不能把好代码改成坏代码；
3. **修不了就说出来**：无法机械修复的规则（复杂度、``eval`` 等）不会被静默忽略，
   而是进入 ``manual_items`` 清单，由报告原样呈现给用户。

另外，修复 Agent 会统计「本轮实际改了几处」。如果一处都没改（例如问题全都
不在自动修复范围内），流程条件会据此结束循环——**这是防止两个 Agent 互相
空转的关键**：没有进展就必须停，而不是耗光最大轮次。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from codereview.report import Issue

from ..node import NodeContext, NodeResult
from .base import Agent, append_history
from .repairs import FIXERS, MANUAL_RULES, SourceEditor

MAX_FIXES_PER_ROUND = 60


class FixerAgent(Agent):
    """按审查报告做确定性修复。

    **每轮改动上限**（``fix_budget``，默认不限）：真实项目里一次改几十处代码
    是没法 review 的，所以支持「每轮最多改 N 处，复审后再继续」。它也顺带让
    「多轮循环」这件事可以被真实验证——不是靠人为拖延，而是靠一个有意义的约束。
    """

    name = "fixer"
    role = "修复 Agent：按规则做确定性修复，并校验语法"
    reads = ("code_path", "findings", "round", "workspace", "fix_budget")
    writes = ("code", "fix_log", "manual_items", "round", "code_history")

    def act(self, ctx: NodeContext) -> NodeResult:
        rel_path = str(ctx.state.require("code_path", str))
        workspace = Path(ctx.state.require("workspace"))
        target = workspace / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"待修复文件不存在：{target}")

        findings = list(ctx.state.get("findings") or [])
        if not findings:
            ctx.log("没有待修复的问题，跳过本轮")
            return NodeResult(
                summary="无问题可修",
                artifacts={"fix_log": {"applied": 0, "skipped": 0, "manual": 0, "deferred": 0, "items": []},
                           "manual_items": [], "round": int(ctx.state.get("round") or 1)},
            )

        budget = int(ctx.state.get("fix_budget") or 0)  # 0 表示不限
        original = target.read_text(encoding="utf-8")
        editor = SourceEditor(original)
        items: list[dict[str, Any]] = []
        manual: list[dict[str, Any]] = []
        applied = skipped = deferred = 0

        # 按行号从小到大处理：同一行的冲突由先到者胜出，后来者会被记录原因
        for finding in sorted(findings, key=lambda item: int(item.get("line") or 0)):
            rule = str(finding.get("rule") or "")
            line = int(finding.get("line") or 0)
            issue = Issue.from_dict(finding)
            title = issue.title if issue else str(finding.get("title", ""))

            fixer = FIXERS.get(rule)
            if fixer is None:
                manual.append({
                    "rule": rule or "(未标注规则)",
                    "line": line,
                    "title": title,
                    "severity": finding.get("severity", "minor"),
                    "reason": MANUAL_RULES.get(rule, "目前没有对应的自动修复规则，需要人工判断"),
                })
                continue

            if budget and applied >= budget:
                deferred += 1
                continue

            outcome = fixer(editor, issue)
            items.append(outcome.to_dict())
            if outcome.applied:
                applied += 1
            else:
                skipped += 1

        if editor.skipped:
            items.extend(
                {"rule": "-", "line": 0, "applied": False, "description": "", "reason": reason}
                for reason in editor.skipped
            )

        new_code = editor.render()
        round_no = int(ctx.state.get("round") or 1)

        # ---- 安全闸门：改完必须还能编译 ----
        syntax_error = self._syntax_error(new_code)
        if syntax_error:
            ctx.log(f"修复后语法校验失败，已整轮丢弃：{syntax_error}")
            return NodeResult(
                summary=f"修复被回滚（语法校验失败）：{syntax_error}",
                artifacts={
                    "fix_log": {"applied": 0, "skipped": skipped, "manual": len(manual),
                                "items": items, "rolled_back": True, "reason": syntax_error},
                    "manual_items": manual,
                    "round": round_no,
                },
                detail={"rolled_back": True},
            )

        target.write_text(new_code, encoding="utf-8")
        history = append_history(ctx, "code_history", {
            "round": round_no + 1, "by": self.name, "reason": f"第 {round_no} 轮修复",
            "path": rel_path, "chars": len(new_code), "fixes": applied,
        })

        log = {
            "applied": applied,
            "skipped": skipped,
            "manual": len(manual),
            "deferred": deferred,
            "budget": budget,
            "items": items[:MAX_FIXES_PER_ROUND],
            "rolled_back": False,
        }
        ctx.send(
            "reviewer", "fix_applied",
            summary=(f"第 {round_no} 轮修复：改动 {applied} 处，"
                     f"{len(manual)} 项需人工处理" + (f"，{deferred} 处留待下一轮" if deferred else "")),
            round=round_no, applied=applied, manual=len(manual), deferred=deferred,
        )
        ctx.log(f"第 {round_no} 轮修复完成：改动 {applied} 处，跳过 {skipped} 处，"
                f"需人工 {len(manual)} 项" + (f"，留待下一轮 {deferred} 处" if deferred else ""))

        return NodeResult(
            summary=(f"第 {round_no} 轮修复：改动 {applied} 处"
                     + (f"（受每轮上限 {budget} 处限制，{deferred} 处顺延）" if deferred else "")
                     + f"，需人工 {len(manual)} 项"),
            artifacts={
                "code": new_code,
                "code_history": history,
                "fix_log": log,
                "manual_items": manual,
                "round": round_no + 1,
            },
            detail={"applied": applied, "manual": len(manual), "deferred": deferred},
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _syntax_error(code: str) -> str:
        """返回语法错误说明；没有错误则返回空串。"""
        try:
            ast.parse(code)
            return ""
        except SyntaxError as exc:
            return f"第 {exc.lineno} 行 {exc.msg}"


__all__ = ["FixerAgent"]
