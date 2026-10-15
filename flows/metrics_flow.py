"""第二条流程：代码度量报告（**用并行节点**）。

存在的理由有两个，都很实际：

1. **让「并行执行」不只是一个 API**：审查流程天然是顺序的，硬塞并行进去是为了
   演示而演示。这条流程不一样——行数统计、复杂度度量、待办扫描三者互不依赖，
   并行执行是**顺理成章**的选择。
2. **证明可扩展性**：新增一条流程只写了这个文件，``workflow/`` 引擎**一行都没改**。
   这正是作业「架构是否支持新增 Agent、修改流程」想要看到的。

图中的并行结构：

::

    开始 ─▶ 扫描文件 ─┬─▶ 统计行数      ─┬─▶ 汇总报告 ─▶ 结束
                      ├─▶ 度量复杂度     ─┤
                      └─▶ 扫描 TODO      ─┘
                        （三个子节点并行）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codereview.tools.python_ast import PythonAnalyzer

from workflow.agents import AgentRuntime
from workflow.agents.repairs import SourceEditor  # noqa: F401  (占位：保持与审查流程一致的导入风格)
from workflow.graph import END, START, Workflow
from workflow.node import FunctionNode, NodeContext, NodeResult, ParallelNode

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "runs", ".mypy_cache", ".ruff_cache"}
TODO_RE = re.compile(r"#.*\b(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.I)


def _python_files(workspace: Path) -> list[Path]:
    """列出工作区里的 Python 文件（跳过缓存与运行产物目录）。"""
    found: list[Path] = []
    for path in sorted(workspace.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


# --------------------------------------------------------------------------- #
# 节点实现
# --------------------------------------------------------------------------- #
def scan_files(ctx: NodeContext) -> NodeResult:
    """节点 1：扫描待分析目录，产出文件清单。

    扫描对象是 ``scan_root``（由 ``--scan`` 指定），默认当前目录——
    而不是运行产物目录，否则每次都会扫到一个空文件夹。
    """
    workspace = Path(ctx.state.require("workspace"))
    scan_root = Path(ctx.state.get("scan_root") or Path.cwd())
    if not scan_root.is_dir():
        raise FileNotFoundError(f"待分析目录不存在：{scan_root}")
    files = [path for path in _python_files(scan_root) if workspace not in path.parents]
    rel = [path.relative_to(scan_root).as_posix() for path in files]
    ctx.log(f"扫描 {scan_root} 得到 {len(rel)} 个 Python 文件")
    return NodeResult(
        summary=f"扫描到 {len(rel)} 个 Python 文件（{scan_root.name}）",
        artifacts={"files": rel, "scan_root": str(scan_root)},
    )


def _read(ctx: NodeContext, rel_path: str) -> str:
    scan_root = Path(ctx.state.get("scan_root") or Path.cwd())
    return (scan_root / rel_path).read_text(encoding="utf-8")


def count_lines(ctx: NodeContext) -> NodeResult:
    """并行子节点 A：统计总行数、空行与注释行。"""
    total = blank = comment = 0
    for rel_path in ctx.state.require("files", list):
        for line in _read(ctx, rel_path).splitlines():
            total += 1
            stripped = line.strip()
            if not stripped:
                blank += 1
            elif stripped.startswith("#"):
                comment += 1
    stats = {"total": total, "blank": blank, "comment": comment,
             "code": total - blank - comment}
    ctx.log(f"行数统计：{total} 行（代码 {stats['code']}）")
    return NodeResult(summary=f"{total} 行（代码 {stats['code']}）", artifacts={"lines_stats": stats})


def measure_complexity(ctx: NodeContext) -> NodeResult:
    """并行子节点 B：用**项目1 的分析器**度量复杂度。"""
    complexities: dict[str, int] = {}
    functions = 0
    for rel_path in ctx.state.require("files", list):
        report = PythonAnalyzer(_read(ctx, rel_path), rel_path).analyze()
        functions += int(report.metrics.get("functions", 0) or 0)
        for function in report.functions:
            complexities[f"{rel_path}::{function.name}"] = function.complexity

    values = list(complexities.values()) or [0]
    worst = sorted(complexities.items(), key=lambda item: -item[1])[:3]
    stats = {
        "functions": functions,
        "max": max(values),
        "avg": round(sum(values) / len(values), 2),
        "over_10": sum(1 for value in values if value > 10),
        "worst": [{"name": name, "complexity": value} for name, value in worst],
    }
    ctx.log(f"复杂度度量：{functions} 个函数，最高 {stats['max']}")
    return NodeResult(summary=f"{functions} 个函数，最高复杂度 {stats['max']}",
                      artifacts={"complexity_stats": stats})


def find_todos(ctx: NodeContext) -> NodeResult:
    """并行子节点 C：扫描遗留的 TODO / FIXME。"""
    hits: list[dict[str, Any]] = []
    for rel_path in ctx.state.require("files", list):
        for number, line in enumerate(_read(ctx, rel_path).splitlines(), start=1):
            match = TODO_RE.search(line)
            if match:
                hits.append({"file": rel_path, "line": number,
                             "kind": match.group(1).upper(), "text": match.group(2).strip()[:80]})
    ctx.log(f"待办扫描：{len(hits)} 处")
    return NodeResult(summary=f"{len(hits)} 处待办", artifacts={"todo_stats": hits})


def summarize(ctx: NodeContext) -> NodeResult:
    """节点 3：把三路并行结果汇成 Markdown 报告。"""
    lines_stats = ctx.state.require("lines_stats", dict)
    complexity = ctx.state.require("complexity_stats", dict)
    todos = ctx.state.require("todo_stats", list)

    report = [
        "# 代码度量报告",
        "",
        f"> 分析目录：`{ctx.state.get('scan_root')}`",
        "> 由 `metrics-parallel` 流程生成：扫描 → 并行分析（行数 / 复杂度 / 待办）→ 汇总。",
        "",
        "## 一、规模",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 文件数 | {len(ctx.state.get('files') or [])} |",
        f"| 总行数 | {lines_stats['total']} |",
        f"| 代码行 | {lines_stats['code']} |",
        f"| 注释行 | {lines_stats['comment']} |",
        f"| 空行 | {lines_stats['blank']} |",
        "",
        "## 二、复杂度（分析器复用项目1）",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 函数总数 | {complexity['functions']} |",
        f"| 最高圈复杂度 | {complexity['max']} |",
        f"| 平均圈复杂度 | {complexity['avg']} |",
        f"| 复杂度 > 10 的函数 | {complexity['over_10']} |",
        "",
    ]
    if complexity["worst"]:
        report.append("最复杂的三个函数：")
        report.append("")
        for item in complexity["worst"]:
            report.append(f"- `{item['name']}` —— 复杂度 {item['complexity']}")
        report.append("")

    report.append(f"## 三、遗留待办（{len(todos)} 处）")
    report.append("")
    if todos:
        report.append("| 位置 | 类型 | 说明 |")
        report.append("| --- | --- | --- |")
        for item in todos[:20]:
            report.append(f"| `{item['file']}:{item['line']}` | {item['kind']} | {item['text'] or '—'} |")
    else:
        report.append("没有发现 TODO / FIXME 标记。")

    text = "\n".join(report) + "\n"
    workspace = Path(ctx.state.require("workspace"))
    target = workspace / "metrics-report.md"
    target.write_text(text, encoding="utf-8")
    ctx.log(f"度量报告已写入 {target.name}")

    return NodeResult(
        summary=f"度量报告已生成（{lines_stats['total']} 行代码，{len(todos)} 处待办）",
        artifacts={"metrics_report": text},
        detail={"terminal_reason": "度量报告生成完毕"},
    )


# --------------------------------------------------------------------------- #
def build_metrics_workflow(runtime: AgentRuntime, **_ignored: Any) -> Workflow:
    """构建代码度量流程（``--flow metrics-parallel``）。

    ``**_ignored`` 用于吞掉 CLI 统一传入的 ``max_rounds`` / ``target_score``——
    这条流程没有修复循环，用不到它们。
    """
    workflow = Workflow(
        "metrics-parallel",
        description="代码度量：扫描 → 并行分析（行数/复杂度/待办）→ 汇总",
    )
    workflow.add(
        FunctionNode("scan", scan_files, description="扫描工作区里的 Python 文件", writes=("files",)),
        ParallelNode(
            "analyze",
            [
                FunctionNode("count_lines", count_lines, description="统计行数与注释率", writes=("lines_stats",)),
                FunctionNode("measure_complexity", measure_complexity,
                             description="用项目1 的分析器度量复杂度", writes=("complexity_stats",)),
                FunctionNode("find_todos", find_todos, description="扫描 TODO / FIXME", writes=("todo_stats",)),
            ],
            description="三路指标并行分析（互不依赖）",
            writes=("lines_stats", "complexity_stats", "todo_stats"),
        ),
        FunctionNode("summarize", summarize, description="汇总成 Markdown 度量报告", writes=("metrics_report",)),
    )
    workflow.connect(START, "scan", label="工作区就绪")
    workflow.connect("scan", "analyze", label="文件清单就绪，开始并行分析")
    workflow.connect("analyze", "summarize", label="三路指标到齐")
    workflow.connect("summarize", END, label="度量报告生成")
    return workflow


__all__ = ["build_metrics_workflow", "SKIP_DIRS"]
