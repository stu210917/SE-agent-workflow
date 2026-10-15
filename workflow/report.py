"""运行报告：把一次 Workflow 执行渲染成可交付的 Markdown。

报告回答四个问题：

1. **跑通了吗**——终止原因、总步数、耗时、最终评分；
2. **Agent 之间怎么协作的**——每个节点执行了几次、消息流水长什么样；
3. **改了什么**——每轮的评分演化、修复明细、以及**修不了的问题**；
4. **怎么复现**——完整的事件日志（JSONL）与最终代码。

刻意与项目1 的 ``ReviewReport`` 保持一致的风格（表格 + 分节 + 附录），
因为这两个项目会一起提交，读者不该在两份报告之间切换阅读习惯。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Sequence

from .agents.repairs import FIXERS, MANUAL_RULES
from .engine import WorkflowRun
from .graph import Workflow

SEVERITY_COLUMNS = ("critical", "major", "minor", "info")
SEVERITY_LABEL = {"critical": "严重", "major": "重要", "minor": "次要", "info": "提示"}


def render_run_report(
    run: WorkflowRun,
    workflow: Workflow,
    *,
    runtime: Any = None,
    include_code: bool = True,
    include_events: bool = True,
) -> str:
    """渲染 Markdown 报告。"""
    state = run.state
    review: dict[str, Any] = state.get("review") or {}
    counts: dict[str, int] = review.get("counts") or {}
    history: list[dict[str, Any]] = list(state.get("review_history") or [])
    fix_log: dict[str, Any] = state.get("fix_log") or {}
    # 「需人工处理」直接从**最终一轮审查结果**推导，而不是用最后一轮修复时的快照——
    # 后者比最终审查早一轮，会漏掉复审新发现的问题（这是本项目修过的一个报告缺陷）
    manual = [
        {
            "rule": finding.get("rule", ""),
            "line": finding.get("line", 0),
            "title": finding.get("title", ""),
            "severity": finding.get("severity", "minor"),
            "reason": MANUAL_RULES.get(finding.get("rule", ""), "目前没有对应的自动修复规则，需要人工判断"),
        }
        for finding in (state.get("findings") or [])
        if finding.get("rule") not in FIXERS
    ]

    lines: list[str] = ["# Workflow 运行报告", ""]
    lines.append(_meta_line(run, workflow, runtime))
    lines.append("")

    # ------------------------------------------------------------------ #
    lines.append("## 一、结论")
    lines.append("")
    verdict = "✅ 流程跑通" if run.ok else "⚠️ 流程未完全成功"
    lines.append(f"**{verdict}** —— {run.reason}")
    lines.append("")
    lines.append(f"- 执行步数：**{run.steps}**（节点执行 "
                 f"{'、'.join(f'{name}×{count}' for name, count in run.node_visits().items()) or '无'}）")
    lines.append(f"- 修复轮次：**{int(state.get('round') or 1) - 1}** 轮"
                 f"（上限 {state.get('max_rounds')} 轮）")
    lines.append(f"- 最终评分：**{review.get('score', '—')}/100**"
                 f"（{review.get('grade', '—')}）")
    lines.append(f"- 消息总数：**{len(run.bus)}** 条；状态键：**{len(state)}** 个")
    lines.append(f"- 总耗时：**{run.duration_s:.2f}s**")
    lines.append("")

    # ------------------------------------------------------------------ #
    lines.append("## 二、流程图")
    lines.append("")
    lines.append("```mermaid")
    lines.append(workflow.to_mermaid())
    lines.append("```")
    lines.append("")

    # ------------------------------------------------------------------ #
    lines.append("## 三、评分演化（每一轮审查的结果）")
    lines.append("")
    if history:
        lines.append("| 轮次 | 问题总数 | 严重 | 重要 | 次要 | 提示 | 评分 |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for entry in history:
            entry_counts = entry.get("counts") or {}
            lines.append(
                f"| 第 {entry.get('round')} 轮 | {entry.get('total')} "
                f"| {entry_counts.get('critical', 0)} | {entry_counts.get('major', 0)} "
                f"| {entry_counts.get('minor', 0)} | {entry_counts.get('info', 0)} "
                f"| **{entry.get('score')}** |"
            )
        lines.append("")
        first, last = history[0], history[-1]
        delta = int(last.get("score", 0)) - int(first.get("score", 0))
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(f"> 评分变化：{first.get('score')} → {last.get('score')}（{arrow} {abs(delta)}），"
                     f"问题数 {first.get('total')} → {last.get('total')}。")
    else:
        lines.append("本次运行没有产生审查记录。")
    lines.append("")

    # ------------------------------------------------------------------ #
    lines.append("## 四、修复明细")
    lines.append("")
    items = fix_log.get("items") or []
    applied = [item for item in items if item.get("applied")]
    failed = [item for item in items if not item.get("applied") and item.get("rule") != "-"]

    # fix_log 每轮会被覆盖，因此累计值必须从**历史版本**里取
    rounds = [log for log in state.versions("fix_log") if isinstance(log, dict)]
    total_applied = sum(int(log.get("applied") or 0) for log in rounds)
    total_manual = sum(int(log.get("manual") or 0) for log in rounds)
    total_deferred = sum(int(log.get("deferred") or 0) for log in rounds)

    lines.append(f"整个流程累计自动修复 **{total_applied}** 处，涉及 {len(rounds)} 个修复轮次；"
                 f"最终仍有 **{len(manual)}** 项需人工处理。")
    lines.append("")
    if rounds:
        lines.append("| 轮次 | 本轮改动 | 规则命中但未改动 | 需人工 | 顺延到下一轮 | 是否回滚 |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | :---: |")
        for index, log in enumerate(rounds, start=1):
            lines.append(
                f"| 第 {index} 轮 | {log.get('applied', 0)} | {log.get('skipped', 0)} "
                f"| {log.get('manual', 0)} | {log.get('deferred', 0)} "
                f"| {'⚠️ 是' if log.get('rolled_back') else '否'} |"
            )
        if total_deferred:
            lines.append("")
            lines.append(f"> 「顺延」共 {total_deferred} 处：受每轮改动上限（`--fix-budget`）限制，"
                         "留到下一轮继续——这是有意为之，一次改几十处代码是没法 review 的。")
        lines.append("")
    if fix_log.get("rolled_back"):
        lines.append(f"> ⚠️ **最后一轮修复被回滚**：{fix_log.get('reason')}。"
                     "修复 Agent 在写回文件前会做语法校验，改坏了就整轮丢弃，避免把能跑的代码改成跑不起来的。")
        lines.append("")
    if applied:
        lines.append("### 最后一轮已自动修复")
        lines.append("")
        lines.append("| 位置 | 规则 | 改动 |")
        lines.append("| --- | --- | --- |")
        for item in applied:
            lines.append(f"| 第 {item.get('line')} 行 | `{item.get('rule')}` | {item.get('description')} |")
        lines.append("")
    if failed:
        lines.append("### 最后一轮命中规则但未改动")
        lines.append("")
        lines.append("| 位置 | 规则 | 原因 |")
        lines.append("| --- | --- | --- |")
        for item in failed:
            lines.append(f"| 第 {item.get('line')} 行 | `{item.get('rule')}` | {item.get('reason') or '—'} |")
        lines.append("")
    if manual:
        lines.append("### 需人工处理（不在自动修复范围内）")
        lines.append("")
        lines.append("| 位置 | 严重度 | 问题 | 为什么不能自动修 |")
        lines.append("| --- | --- | --- | --- |")
        for item in manual:
            label = SEVERITY_LABEL.get(item.get("severity", ""), item.get("severity", ""))
            lines.append(f"| 第 {item.get('line')} 行 | {label} | {item.get('title')} | {item.get('reason')} |")
        lines.append("")

    # ------------------------------------------------------------------ #
    lines.append("## 五、Agent 协作记录")
    lines.append("")
    lines.append("消息按发生顺序排列，体现「谁让谁做什么」：")
    lines.append("")
    lines.append("```")
    lines.append(run.bus.render(limit=40))
    lines.append("```")
    lines.append("")
    lines.append("共享状态被写入的顺序（黑板的记账流水）：")
    lines.append("")
    lines.append("```")
    lines.append(state.render_changes(limit=30))
    lines.append("```")
    lines.append("")

    # ------------------------------------------------------------------ #
    if include_code:
        code = state.get("code") or ""
        if code:
            lines.append("## 六、最终代码")
            lines.append("")
            lines.append(f"`runs/…/{state.get('code_path', 'solution.py')}`（{len(code.splitlines())} 行）")
            lines.append("")
            lines.append("```python")
            lines.append(code.rstrip())
            lines.append("```")
            lines.append("")

    # ------------------------------------------------------------------ #
    if include_events:
        lines.append("## 附录 A：执行事件（JSONL）")
        lines.append("")
        lines.append("```json")
        lines.append(run.recorder.to_jsonl())
        lines.append("```")
        lines.append("")
        lines.append("## 附录 B：完整消息存档")
        lines.append("")
        lines.append("```json")
        lines.append(run.bus.to_json())
        lines.append("```")
        lines.append("")
        lines.append("## 附录 C：运行结果（机器可读）")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(run.to_dict(), ensure_ascii=False, indent=2, default=str))
        lines.append("```")
    return "\n".join(lines)


def _meta_line(run: WorkflowRun, workflow: Workflow, runtime: Any) -> str:
    parts = [f"流程 `{workflow.name}`"]
    if runtime is not None:
        parts.append(f"模式：{runtime.mode_label}")
        parts.append(f"模型：`{runtime.config.model}`")
    parts.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "> " + " ｜ ".join(parts)


def summarize_run(run: WorkflowRun) -> str:
    """一行结论（CLI 末尾打印）。

    刻意不写死「评分」：度量流程这类没有审查环节的流程不该显示一个「—」，
    那只会让读者以为是出错了。
    """
    state = run.state
    review: dict[str, Any] = state.get("review") or {}
    parts = [f"{'成功' if run.ok else '未完成'}：{run.reason}", f"{run.steps} 步"]
    rounds = int(state.get("round") or 1) - 1
    if rounds > 0:
        parts.append(f"{rounds} 轮修复")
    if review:
        parts.append(f"最终评分 {review.get('score', '—')}")
    return "；".join(parts)


__all__ = ["render_run_report", "summarize_run", "SEVERITY_LABEL", "SEVERITY_COLUMNS"]
