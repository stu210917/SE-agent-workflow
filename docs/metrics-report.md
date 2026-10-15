# 代码度量报告

> 分析目录：`D:\test\code-review-workflow`
> 由 `metrics-parallel` 流程生成：扫描 → 并行分析（行数 / 复杂度 / 待办）→ 汇总。

## 一、规模

| 指标 | 数值 |
| --- | ---: |
| 文件数 | 45 |
| 总行数 | 10395 |
| 代码行 | 8314 |
| 注释行 | 389 |
| 空行 | 1692 |

## 二、复杂度（分析器复用项目1）

| 指标 | 数值 |
| --- | ---: |
| 函数总数 | 536 |
| 最高圈复杂度 | 47 |
| 平均圈复杂度 | 3.43 |
| 复杂度 > 10 的函数 | 35 |

最复杂的三个函数：

- `workflow/report.py::render_run_report` —— 复杂度 47
- `codereview/agent.py::parse_action` —— 复杂度 25
- `codereview/tools/base.py::validate` —— 复杂度 25

## 三、遗留待办（6 处）

| 位置 | 类型 | 说明 |
| --- | --- | --- |
| `codereview/tools/python_ast.py:75` | HACK | )\b[:\s]*(.*)", re.I) |
| `flows/metrics_flow.py:35` | HACK | )\b[:\s]*(.*)", re.I) |
| `tests/test_metrics_flow.py:38` | TODO | 补一个默认分支 |
| `workflow/agents/coder.py:103` | TODO | 支持从标准输入读取，并补一份使用示例 |
| `workflow/agents/coder.py:180` | FIXME | 需要支持环境变量覆盖与 .env 文件 |
| `workflow/agents/coder.py:228` | TODO | 补充参数解析与错误处理 |
