"""命令行界面：``run`` / ``graph`` / ``agents`` / ``rules`` 四个子命令。

延续项目1 的取舍：

* **实时反馈**：引擎抛出的每条事件立刻渲染到终端，而不是等流程跑完才吐结果——
  Workflow 类项目最有说服力的画面就是「节点一个个亮起来」；
* **一条命令跑通**：不传 ``--spec`` 时用内置示例需求，clone 下来直接
  ``python run.py run`` 就能看到完整的「编码 → 审查 → 修复」；
* **CLI 友好**：``--json`` 让 stdout 只输出机器可读结果，``--out`` 导出 Markdown 报告。

退出码
------
====  ==========================================
码值   含义
====  ==========================================
0     流程正常收敛
1     流程跑完但未完全成功（如轮次用尽仍有严重问题）
2     参数或配置错误
130   用户中断（Ctrl+C）
====  ==========================================
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

# ---- 复用项目1 的通用能力：UTF-8 输出、终端着色、配置体系 ----
from codereview.cli import make_style
from codereview.utils import enable_utf8_output

# 注意：`flows` 与 `workflow` 是并列的顶层包，因此这里必须用绝对导入
from flows import FLOWS, initial_state

from . import __version__
from .agents import AgentRuntime
from .agents.repairs import FIXERS, MANUAL_RULES
from .engine import EngineConfig, WorkflowEngine
from .errors import WorkflowError
from .events import render_console
from .graph import Workflow
from .report import render_run_report, summarize_run

EXIT_OK = 0
EXIT_UNFINISHED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

#: 内置示例需求：不传 --spec 时使用，保证「一条命令就能看到效果」
DEFAULT_SPEC = """\
写一个记录导出小工具：
1. 从 CSV 文件读取记录；
2. 支持按条件过滤、限制条数；
3. 把结果导出为 JSON 文件；
4. 需要能单独统计记录条数与字段数。
"""


def _load_flow(name: str, runtime: AgentRuntime, *, max_rounds: int, target_score: int) -> Workflow:
    builder = FLOWS.get(name)
    if builder is None:
        raise WorkflowError(f"未知流程 `{name}`；可用：{sorted(FLOWS)}")
    return builder(runtime, max_rounds=max_rounds, target_score=target_score)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    enable_utf8_output()

    spec = _resolve_spec(args)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else _default_workspace()
    workspace.mkdir(parents=True, exist_ok=True)

    runtime = AgentRuntime.create(
        workspace=workspace,
        mock=True if args.mock else None,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        max_steps=args.review_steps,
        color=False if args.no_color else None,
    )
    style = make_style(runtime.config)
    workflow = _load_flow(args.flow, runtime, max_rounds=args.max_rounds, target_score=args.target_score)

    # --json 时 stdout 必须只有 JSON（供管道/CI 消费），因此把横幅与进度写到 stderr
    diagnostics = sys.stderr if args.json else sys.stdout

    def say(text: str = "") -> None:
        print(text, file=diagnostics)

    say(style.bold(f"多 Agent Workflow v{__version__}"))
    say(f"  流程    : {workflow.name} —— {workflow.description}")
    say(f"  模式    : {runtime.mode_label}")
    say(f"  工作区  : {workspace}")
    budget_text = "不限" if not args.fix_budget else f"{args.fix_budget} 处/轮"
    say(f"  上限    : 最多 {args.max_rounds} 轮修复，目标评分 {args.target_score}，单轮改动 {budget_text}")
    say(f"  需求    : {spec.strip().splitlines()[0][:60]}…")
    if runtime.mock:
        say(style.yellow("  提示    : 未检测到 API Key —— 编码走内置模板、修复走确定性规则，审查仍用 AST 引擎"))
    say()

    for warning in workflow.warnings():
        say(style.yellow(f"  ⚠ {warning}"))

    listeners: list[Any] = []
    if not args.quiet:
        listeners.append(lambda event: print(render_console(event, style=style), file=diagnostics))

    engine = WorkflowEngine(
        workflow,
        config=EngineConfig(max_steps=args.max_steps),
        node_config=runtime,
        listeners=listeners,
    )
    if listeners:
        say(style.dim("─" * 72))

    started = time.perf_counter()
    try:
        state = initial_state(
            spec, workspace,
            max_rounds=args.max_rounds, target_score=args.target_score,
            module_name=args.module, fix_budget=args.fix_budget,
        )
        if getattr(args, "scan", None):
            state["scan_root"] = str(Path(args.scan).expanduser().resolve())
        run = engine.run(state)
    except KeyboardInterrupt:
        print("\n已中断。")
        return EXIT_INTERRUPTED
    duration = time.perf_counter() - started

    # ---- 结果输出 ----
    report_path = Path(args.out).expanduser() if args.out else workspace / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_run_report(run, workflow, runtime=runtime, include_code=not args.no_code),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        say(style.bold("─" * 72))
        say(style.bold("流程结果") + f"：{summarize_run(run)}")
        say(f"  累计耗时：{duration:.2f}s")
        say(f"  报告已写入：{report_path}")
        code_path = run.state.get("code_path")
        if code_path:
            say(f"  最终代码：{workspace / code_path}")
        manual = run.state.get("manual_items") or []
        if manual:
            say(style.yellow(f"  需人工处理：{len(manual)} 项（详见报告第四节）"))
        say()
        say(style.dim("提示：打开报告可以看到评分演化、Agent 消息流水与完整修复明细。"))

    return EXIT_OK if run.ok else EXIT_UNFINISHED


def cmd_graph(args: argparse.Namespace) -> int:
    enable_utf8_output()
    runtime = AgentRuntime.create(workspace=Path.cwd(), mock=True)
    workflow = _build(args, runtime)
    if args.format == "mermaid":
        print(workflow.to_mermaid())
    else:
        print(workflow.to_ascii())
        warnings = workflow.warnings()
        if warnings:
            print("\n提示：")
            for warning in warnings:
                print(f"  ⚠ {warning}")
    return EXIT_OK


def cmd_agents(args: argparse.Namespace) -> int:
    enable_utf8_output()
    runtime = AgentRuntime.create(workspace=Path.cwd(), mock=True)
    workflow = _build(args, runtime)
    print(f"流程 `{workflow.name}` 共 {len(workflow)} 个 Agent 节点：\n")
    for node in workflow.nodes():
        info = node.describe()
        print(f"● {info['name']}  [{info['kind']}]  {info['description']}")
        if info.get("agent"):
            print(f"    实现类  : {info['agent']}")
        print(f"    读取状态: {', '.join(info['reads']) or '（无）'}")
        print(f"    写入状态: {', '.join(info['writes']) or '（无）'}")
        if info.get("retries"):
            print(f"    失败重试: {info['retries']} 次")
        if info.get("on_error"):
            print(f"    错误恢复: → {info['on_error']}")
        print()
    print("Agent 之间不互相引用：它们只声明「读什么、写什么」，由共享状态与消息总线协作。")
    return EXIT_OK


def cmd_rules(args: argparse.Namespace) -> int:
    enable_utf8_output()
    print(f"可自动修复的规则（{len(FIXERS)} 条）：\n")
    for rule, fixer in sorted(FIXERS.items()):
        doc = (fixer.__doc__ or "").strip().splitlines()[0] if fixer.__doc__ else ""
        print(f"  ● {rule:<20} {doc}")
    print(f"\n需人工处理的规则（{len(MANUAL_RULES)} 条）——修复 Agent 会原样列进报告，不会静默忽略：\n")
    for rule, reason in sorted(MANUAL_RULES.items()):
        print(f"  ○ {rule:<24} {reason}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _build(args: argparse.Namespace, runtime: AgentRuntime) -> Workflow:
    """按命令行参数构建流程（graph / agents 子命令用，不需要真的跑）。"""
    return _load_flow(
        args.flow, runtime,
        max_rounds=getattr(args, "max_rounds", 3),
        target_score=getattr(args, "target_score", 85),
    )


def _resolve_spec(args: argparse.Namespace) -> str:
    if args.spec_file:
        path = Path(args.spec_file).expanduser()
        if not path.is_file():
            raise WorkflowError(f"需求文件不存在：{path}")
        return path.read_text(encoding="utf-8")
    if args.spec:
        return args.spec
    return DEFAULT_SPEC


def _default_workspace() -> Path:
    return Path.cwd() / "runs" / time.strftime("run-%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("流程")
    group.add_argument("--flow", default="review-loop", help="流程名，默认 review-loop")
    group.add_argument("--max-rounds", dest="max_rounds", type=int, default=3,
                       help="最多修复几轮，默认 3")
    group.add_argument("--target-score", dest="target_score", type=int, default=85,
                       help="目标评分，达到即结束，默认 85")
    group.add_argument("--fix-budget", dest="fix_budget", type=int, default=0,
                       help="每轮最多自动改几处（0=不限）。设小可以看到多轮循环逐步收敛")
    group.add_argument("--max-steps", dest="max_steps", type=int, default=40,
                       help="引擎步数硬上限（循环保护），默认 40")

    group = parser.add_argument_group("LLM 接入（与项目1 共用同一套配置）")
    group.add_argument("--mock", action="store_true", help="强制离线模式（不联网、不需要 API Key）")
    group.add_argument("--model", help="模型名，如 deepseek-chat / gpt-4o-mini")
    group.add_argument("--base-url", dest="base_url", help="OpenAI 兼容接口地址")
    group.add_argument("--api-key", dest="api_key", help="API Key（也可用 CR_API_KEY 环境变量）")
    group.add_argument("--review-steps", dest="review_steps", type=int, default=None,
                       help="审查 Agent 的最大推理步数，默认沿用项目1 配置")
    group.add_argument("--no-color", dest="no_color", action="store_true", help="关闭彩色输出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="多 Agent 协作 Workflow：编码 → 审查 → 修复（审查节点复用项目1 的代码审查 Agent）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python run.py run                                  # 用内置示例需求跑完整流程
  python run.py run --spec "写一个 CSV 去重工具"       # 自定义需求
  python run.py run --max-rounds 5 --out report.md   # 最多修 5 轮，报告另存
  python run.py run --base-url https://api.deepseek.com/v1 --api-key sk-xxx --model deepseek-chat
  python run.py graph --format mermaid               # 导出流程图
  python run.py agents                               # 查看 Agent 的读写状态声明
  python run.py rules                                # 可自动修复 / 需人工的规则清单
""",
    )
    parser.add_argument("--version", action="version", version=f"agent-workflow {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="执行流程")
    run.add_argument("--spec", help="需求文本")
    run.add_argument("--spec-file", dest="spec_file", help="从文件读取需求")
    run.add_argument("--workspace", help="运行目录（产物与报告写在这里），默认 runs/run-时间戳")
    run.add_argument("--module", default="solution", help="生成代码的模块名，默认 solution")
    run.add_argument("--scan", help="度量流程（--flow metrics-parallel）要分析的目录，默认当前目录")
    run.add_argument("--out", help="报告输出路径，默认 <workspace>/report.md")
    run.add_argument("--json", action="store_true", help="stdout 只输出机器可读的 JSON")
    run.add_argument("--no-code", dest="no_code", action="store_true", help="报告里不附最终代码")
    run.add_argument("--quiet", action="store_true", help="不打印实时事件")
    _add_common(run)
    run.set_defaults(func=cmd_run)

    graph = subparsers.add_parser("graph", help="打印流程图")
    graph.add_argument("--format", choices=["ascii", "mermaid"], default="ascii")
    _add_common(graph)
    graph.set_defaults(func=cmd_graph)

    agents = subparsers.add_parser("agents", help="列出 Agent 及其读写状态")
    _add_common(agents)
    agents.set_defaults(func=cmd_agents)

    rules = subparsers.add_parser("rules", help="列出修复规则")
    _add_common(rules)
    rules.set_defaults(func=cmd_rules)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args) or EXIT_OK)
    except WorkflowError as exc:
        # 能冒泡到这里的 WorkflowError 都是「流程还没跑起来」的问题（流程名错、需求文件缺失等）
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # pragma: no cover - 兜底
        print(f"未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNFINISHED


__all__ = ["main", "build_parser", "DEFAULT_SPEC"]
