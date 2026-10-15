"""流程定义：**编码 → 审查 → 修复 → 复审**（带条件收敛的循环）。

::

    开始 ─▶ 编码 ─▶ 审查 ─┬─▶ 结束（已达标 / 无进展 / 轮次用尽 / 只剩人工问题）
                          └─▶ 修复 ─▶ 复审（回到审查）

这里体现了两类流程控制：

* **顺序执行**（必须）：编码 → 审查 → 修复，每一步的产物写进共享状态供下一步读取；
* **条件分支 + 循环**（推荐）：审查之后走「结束」还是「修复」，由 :func:`stop_decision`
  依据评分、严重度分布、轮次、上一轮实际改动数共同决定；修复完回到审查形成闭环。

**为什么把「是否停」写成一个纯函数？**
条件边需要布尔值，报告需要「为什么停」。若各写一份，迟早会不一致。
:func:`stop_decision` 同时返回两者——引擎只用第一个，报告用第二个。
纯函数没有副作用，因此可以放心地在同一轮里被调用多次。
"""

from __future__ import annotations

from typing import Any

# `flows` 与 `workflow` 是并列的顶层包，因此这里用绝对导入
from workflow.agents import AgentRuntime, CoderAgent, FixerAgent, ReviewerAgent
from workflow.agents.repairs import FIXERS
from workflow.graph import END, START, Workflow
from workflow.node import AgentNode
from workflow.state import WorkflowState

#: 默认最大轮次（编码 1 轮 + 修复 N-1 轮）
DEFAULT_MAX_ROUNDS = 3
#: 默认目标评分：达到即可结束
DEFAULT_TARGET_SCORE = 85


def stop_reason(
    state: WorkflowState,
    *,
    default_max_rounds: int = DEFAULT_MAX_ROUNDS,
    default_target_score: int = DEFAULT_TARGET_SCORE,
) -> tuple[str, str]:
    """判断审查后应当结束还是继续修复。

    :return: ``(原因码, 人类可读原因)``。原因码供流程图的条件边使用
             （每条结束边对应一种原因，流程图因此自带「什么情况下会停」的说明）；
             人类可读原因会原样出现在最终报告里。
    """
    review: dict[str, Any] = state.get("review") or {}
    counts: dict[str, int] = review.get("counts") or {}
    score = int(review.get("score") or 0)
    round_no = int(state.get("round") or 1)
    max_rounds = int(state.get("max_rounds") or default_max_rounds)
    target_score = int(state.get("target_score") or default_target_score)
    fix_log: dict[str, Any] = state.get("fix_log") or {}

    if review.get("degraded"):
        return "degraded", f"审查降级交付（{review.get('degraded_reason') or '未完全收敛'}），继续自动修复不可靠"

    if counts.get("critical", 0) == 0 and counts.get("major", 0) == 0:
        minor_info = counts.get("minor", 0) + counts.get("info", 0)
        return "pass", f"已无严重/重要问题（仅剩 {minor_info} 条次要提示），审查通过"

    if score >= target_score:
        return "score", f"评分 {score} 已达到目标 {target_score}"

    if round_no >= max_rounds:
        return "rounds", f"已达到最大轮次 {max_rounds}（避免无限循环）"

    if fix_log.get("rolled_back"):
        return "rollback", f"上一轮修复因语法校验失败被回滚（{fix_log.get('reason', '')}），需要人工介入"

    fixable = sum(1 for item in (state.get("findings") or []) if item.get("rule") in FIXERS)
    if fixable == 0:
        return "manual", "剩余问题都不在自动修复范围内，继续循环没有意义"

    if fix_log and int(fix_log.get("applied") or 0) == 0:
        return "stall", "上一轮修复没有产生任何改动，判定为无法继续自动收敛"

    return "continue", f"仍有 {counts.get('critical', 0) + counts.get('major', 0)} 条严重/重要问题待修复"


def stop_decision(
    state: WorkflowState,
    *,
    default_max_rounds: int = DEFAULT_MAX_ROUNDS,
    default_target_score: int = DEFAULT_TARGET_SCORE,
) -> tuple[bool, str]:
    """``stop_reason`` 的布尔包装：``(是否结束, 原因)``。

    条件边需要布尔值，报告需要「为什么停」。两者共用同一个纯函数，
    因此永远不会出现「流程停了但报告说不清为什么」的情况。
    """
    code, reason = stop_reason(
        state, default_max_rounds=default_max_rounds, default_target_score=default_target_score
    )
    return code != "continue", reason


def build_review_workflow(
    runtime: AgentRuntime,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    target_score: int = DEFAULT_TARGET_SCORE,
    name: str = "code-review-loop",
) -> Workflow:
    """构建「编码 → 审查 → 修复」流程。

    :param runtime: 三个 Agent 共用的运行时（LLM 客户端 + 工作区）。
    :param max_rounds: 最大轮次。优先读初始状态里的 ``max_rounds``，
                       这里是兜底值（防止调用方忘了写）。
    :param target_score: 目标评分，达到即结束；同样以初始状态为准。
    """
    workflow = Workflow(
        name,
        description="编码 → 审查 → 修复 → 复审（条件分支 + 循环收敛）",
    )
    workflow.add(
        AgentNode(CoderAgent(runtime)),
        AgentNode(ReviewerAgent(runtime)),
        AgentNode(FixerAgent(runtime)),
    )
    workflow.connect(START, "coder", label="需求就绪")
    workflow.connect("coder", "reviewer", label="初稿完成，送审")

    def code_of(state: WorkflowState) -> str:
        return stop_reason(state, default_max_rounds=max_rounds, default_target_score=target_score)[0]

    def is_code(*wanted: str):
        return lambda state: code_of(state) in wanted

    # 结束边按「原因」拆开：流程图因此自带「什么情况下会停」的说明，
    # 引擎也会把命中的边标签作为终止原因写进报告（比「走到了终点」有用得多）
    workflow.branch("reviewer", [
        (END, is_code("pass"), "审查通过：已无严重/重要问题"),
        (END, is_code("score"), "评分达标"),
        (END, is_code("rounds"), "达到最大轮次"),
        (END, is_code("manual"), "剩余问题均需人工处理"),
        (END, is_code("stall"), "上一轮修复无进展"),
        (END, is_code("rollback"), "修复被回滚，需人工介入"),
        (END, is_code("degraded"), "审查降级交付"),
        ("fixer", is_code("continue"), "仍有可自动修复的问题"),
    ])
    # 修复被回滚说明「机械修复解决不了」，直接结束等人工介入，不必再跑一轮无效复审
    workflow.branch("fixer", [
        (END, lambda state: bool((state.get("fix_log") or {}).get("rolled_back")), "修复被回滚，需人工介入"),
        ("reviewer", None, "修复已写回，进入复审"),
    ])
    return workflow


def initial_state(
    spec: str,
    workspace: Any,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    target_score: int = DEFAULT_TARGET_SCORE,
    module_name: str = "solution",
    fix_budget: int = 0,
) -> dict[str, Any]:
    """组装流程的初始共享状态。

    :param fix_budget: 每轮最多自动改几处（0 = 不限）。限制单轮改动量是增量重构的
                       常规做法，也让「多轮循环」这件事有真实的用武之地。
    """
    return {
        "spec": spec,
        "workspace": workspace,
        "module_name": module_name,
        "max_rounds": int(max_rounds),
        "target_score": int(target_score),
        "fix_budget": int(fix_budget),
    }


__all__ = [
    "build_review_workflow",
    "stop_decision",
    "stop_reason",
    "initial_state",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_TARGET_SCORE",
]
