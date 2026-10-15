# 多 Agent 协作 Workflow —— 设计文档

> 项目 `agent-workflow` ｜ 版本 `workflow.__version__ = "1.0.0"` ｜ Python 3.9+，**零第三方依赖**（仅标准库）
> 流程：**编码 → 审查 → 修复 → 复审**（条件分支 + 循环收敛）
> 配套材料：[docs/example-run.md](docs/example-run.md)（真实运行报告）｜ [docs/flow.mmd](docs/flow.mmd)（流程图）｜ [docs/example-run.log](docs/example-run.log)（事件日志）

---

## 一、项目概述与作业要求对照

### 1.1 项目目标

作业要求「搭建 2-3 个 Agent 协作的简单 Workflow，理解 Workflow 编排、Agent 间通信、状态管理」。
本项目实现三个 Agent 的**代码质量闭环**：coder 写初稿 → reviewer 静态审查 →
要么结束（通过 / 轮次用尽 / 只剩人工问题），要么 fixer 做确定性修复后回到 reviewer 复审。

选这个题材是因为它天然需要**多轮循环**（改完必须复审）、**条件分支**（停的理由有八种）、
**跨轮次的产物版本**（同一份代码改了三版，报告要说清每轮评分怎么变）——
三件事正好把 Workflow 的核心机制都用上，不必为演示堆砌节点。

### 1.2 作业要求对照

| 作业要求 | 本项目实现 | 代码位置 |
| --- | --- | --- |
| 2-3 个 Agent 协作 | 编码 / 审查 / 修复三个 Agent，各自独立成类 | `workflow/agents/` |
| 至少 2 个 Agent 完成完整流程 | 编码 → 审查 → 修复 → 复审闭环；实测一次 8 步、4 轮审查 | `flows/review_loop.py` |
| 消息传递**或**状态共享 | **两者都做且分工明确**：`MessageBus` 传「意图」，`WorkflowState` 传「产物」 | `workflow/message.py`、`state.py` |
| 基本流程控制（顺序执行） | 引擎按出边顺序推进；`connect()` 声明顺序边 | `workflow/engine.py` |
| MVP 可运行 | `python run.py run` 一条命令跑通，**离线可跑**（无需 API Key） | `run.py` |
| 条件分支 | `Edge.condition` + `priority`；reviewer 有 8 条出边按优先级依次判定 | `workflow/graph.py` |
| 循环 | `fixer → reviewer` 回边；`stop_reason` 判定何时该停 | `flows/review_loop.py` |
| 并行执行 | `ParallelNode`（`ThreadPoolExecutor`）+ 写键冲突检测；**第二条流程 `metrics-parallel` 真实使用** | `workflow/node.py`、`flows/metrics_flow.py` |
| 可视化编排界面 | `to_mermaid()` / `to_ascii()`；`run.py graph`；报告内嵌 Mermaid | `workflow/graph.py`、`cli.py` |
| 执行日志与状态追踪 | 10 类事件 + `EventRecorder` + JSONL 存档 + 状态审计流水 | `workflow/events.py`、`state.py` |
| 错误处理与恢复 | 重试 + 退避、`on_error` 恢复边、失败不静默、修复语法校验回滚 | `workflow/engine.py`、`agents/fixer.py` |
| **从项目1复用 Agent** | `ReviewerAgent` 直接包装项目1 的 `codereview.agent.ReviewAgent`，审查逻辑一行未重写 | `workflow/agents/reviewer.py` |

自研部分 `workflow/`（16 文件 3200 行）+ `flows/`（3 文件 442 行）= **3642 行**，
另有 `tests/` 8 文件 1395 行（143 个用例）；`codereview/` 是项目1 的 vendored 副本（17 文件 5331 行），非本项目编写。

内置两条流程：`review-loop`（编码 → 审查 → 修复 → 复审，条件分支 + 循环）与
`metrics-parallel`（扫描 → 三路并行分析 → 汇总，演示并行执行）。

---

## 二、总体架构

### 2.1 分层结构

```
┌──────────────────────────────────────────────────────────────────────┐
│ 交互层  cli.py（run / graph / agents / rules）  report.py（Markdown） │
│         ← 只消费 WorkflowEvent 与 WorkflowRun，不碰业务逻辑            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ listeners 回调 / run() 返回值
┌──────────────────────────────▼───────────────────────────────────────┐
│ 编排层  engine.py :: WorkflowEngine   ← 一个 step 的生命周期           │
│         graph.py  :: Workflow / Edge  ← 节点 + 条件边                 │
│         events.py :: EventRecorder    ← 执行日志与追踪                │
│         ★ 只认识「节点」与「条件边」，不认识「编码/审查/修复」          │
└───┬────────────────────┬────────────────────┬────────────────────────┘
    │                    │                    │
┌───▼──────────┐  ┌──────▼───────────┐  ┌─────▼──────────────────────┐
│ 节点层        │  │ 状态与通信层      │  │ 流程定义层（业务）          │
│ node.py      │  │ state.py         │  │ flows/review_loop.py       │
│  FunctionNode│  │  WorkflowState   │  │  stop_reason()             │
│  AgentNode   │  │  StateChange     │  │  stop_decision()           │
│  ParallelNode│  │ message.py       │  │  initial_state()           │
│              │  │  MessageBus      │  │  build_review_workflow()   │
└───┬──────────┘  └──────────────────┘  └────────────────────────────┘
    │
    ▼  Agent 层 agents/：coder.py（编码）｜reviewer.py（审查 → vendored codereview）
       fixer.py（修复）｜repairs.py（7 条确定性规则 + 17 条人工规则登记）｜base.py（基类 + AgentRuntime）
       ★ 三个 Agent 之间没有任何直接引用
```

### 2.2 核心决策：引擎与业务解耦

本项目最重要的一条设计线：**`workflow/` 里不出现「编码」「审查」「修复」这些词**。
引擎只认识三件事：节点、带布尔条件的边、共享状态。
「什么问题算修好了」「改几轮该停」全部写在 `flows/review_loop.py` 里，以**条件函数**的形式交给边。

收益是具体的：**换业务不用改引擎**——内置的第二条流程 `metrics-parallel` 就是这么加的：
只新建了 `flows/metrics_flow.py`，`workflow/` 引擎**一行未改**；
**引擎可独立测试**（`tests/test_engine.py` 用「把执行次数记进状态」的假节点，20 个用例 **5 毫秒**跑完，
完全不涉及 LLM）；**停下来这件事只有一份真相**（见 3.3）。

反之，若把 `if 有问题: goto 修复` 写进节点内部，流程就散落在各 Agent 里：
想知道「一共几步」得读完所有节点，改流程得改 Agent，也没法自动画流程图。

---

## 三、Workflow 编排

### 3.1 三类节点

节点是 Workflow 的最小执行单元，全部继承 `Node`（抽象基类，只要求实现 `run(ctx) -> NodeResult`）：

| 节点类型 | 用途 | 要点 | 本项目里的用例 |
| --- | --- | --- | --- |
| `FunctionNode` | 包装普通函数 | 返回 `NodeResult` / `dict` / `None` / 其他值都能被正确解释 | `metrics-parallel` 的 4 个节点 |
| `AgentNode` | 接入一个 Agent | 从 Agent 类属性自动继承 `name`/`role`/`reads`/`writes` | `review-loop` 的 3 个节点 |
| `ParallelNode` | 并发跑若干子节点再汇合 | `ThreadPoolExecutor`；**构造时校验子节点 `writes` 不重叠**，重叠直接抛 `NodeError` | `metrics-parallel` 的 `analyze` 节点 |

**一个刻意的约束**：节点**不直接写共享状态**，而是把产物放进 `NodeResult.artifacts`，由引擎统一写入
（`WorkflowEngine._apply`）。因为只有这样，引擎才能准确记录「谁在第几步写了哪个键、是第几版」；
若允许节点自己 `state.set(...)`，审计日志就会因某个节点偷懒而失真。节点**读**状态不受限制（`ctx.state`）。

节点通过 `NodeContext` 拿到一切：`state`、`bus`、`step`、`node_name`、`config`，
以及 `ctx.send()` / `ctx.broadcast()` / `ctx.log()`。把依赖显式注入而非用全局变量，
是「新增 Agent 不用改引擎」的前提。

### 3.2 条件边与优先级

`Edge` 是数据类：`source` / `target` / `condition` / `label` / `priority`。
`condition` 为 `None` 表示无条件；引擎按 **`priority` 从小到大**求值，**走第一条成立的边**
（`WorkflowEngine._next_node`）；没有边成立则流程结束。
`Workflow.branch(source, cases)` 是批量声明出边的糖，按给定顺序自动分配 `priority`——
所以「先判断什么」在源码里就是从上到下的阅读顺序。reviewer 的出边共 8 条（7 条通向结束 + 1 条通向修复）：

```python
workflow.branch("reviewer", [
    (END, is_code("pass"),     "审查通过：已无严重/重要问题"),
    (END, is_code("score"),    "评分达标"),
    (END, is_code("rounds"),   "达到最大轮次"),
    (END, is_code("manual"),   "剩余问题均需人工处理"),
    # …另有 stall / rollback / degraded 三条结束边，理由见 3.3
    ("fixer", is_code("continue"), "仍有可自动修复的问题"),
])
```

**为什么把每种「停」拆成单独一条边？** 因为边的 `label` 会被引擎当作终止原因写进报告
（`engine.py` 里那句 `reason = ... f"满足结束条件并退出：{label}"`）。
只写一条「到 END」的边，报告就只会说「走到了终点」，而读者想知道的是「**为什么**停」——
实测报告里那句「满足结束条件并退出：剩余问题均需人工处理」就来自这里。

节点还可以在 `NodeResult.detail` 里放一个 `terminal_reason` 覆盖这个文案：引擎读
`result.detail.get("terminal_reason")`（`engine.py` 里出现两次），
`metrics-parallel` 的 `summarize` 节点就用了它——度量流程没有「条件」，直接给出一句
「度量报告生成完毕」比「从 `summarize` 走到终点」更清楚。

### 3.3 stop_reason：停下来这件事只有一份真相

条件边需要布尔值，报告需要「为什么停」。若各写一份判断，迟早不一致（流程停了，报告却说不清原因）。
所以只提供一个纯函数 `stop_reason(state, *, default_max_rounds=3, default_target_score=85)`，
返回 `(原因码, 人类可读原因)`；`stop_decision()` 是它的布尔包装（`code != "continue"`）供条件边使用，
报告直接用那个人类可读原因。八个原因码按判定顺序：

| 原因码 | 触发条件 |
| --- | --- |
| `degraded` | 审查本身是降级交付（`review.degraded`）——审查结果不可信时，继续自动修复没有意义 |
| `pass` | 严重 + 重要问题都为 0（真正的成功条件） |
| `score` | 评分 ≥ `target_score`（默认 85） |
| `rounds` | `round >= max_rounds`（防无限循环的兜底） |
| `rollback` | 上一轮修复因语法校验失败被回滚——机械修复解决不了，再跑也是白跑 |
| `manual` | 剩余问题没有一条在 `FIXERS` 覆盖范围内——修不了就别装作能修（第七章） |
| `stall` | 上一轮修复 `applied == 0`——没有进展就停，**不要耗光轮次** |
| `continue` | 以上都不成立（唯一会走向修复分支的码） |

`rollback` 与 `stall` 放在 `manual` 之后、`continue` 之前，确保「能继续」永远是最后一道判断。
`tests/test_flow.py::StopDecisionTests` 用 8 个用例逐一覆盖。

### 3.4 流程校验：硬错误 vs 软警告

`Workflow.validate()` 在引擎构造时调用（`WorkflowEngine.__init__` 第一行），只检查**一定能判定**的问题，
不通过直接抛 `GraphError`：没有节点、显式入口不存在、缺少起点边、`END` 有出边 / `START` 有入边、
边的端点指向不存在的节点、节点的 `on_error` 指向不存在的节点。
`Workflow.warnings()` 给出**能跑但多半不是你想要的**软性问题，只打印不阻止：从入口不可达的节点、
没有出边也没有 `on_error` 的节点（流程会在此意外结束）。

**为什么不校验节点声明的 `reads`？** 初始状态是运行时注入的（`spec`、`workspace`），静态检查看不到。
只做「边的两端必须存在」这类一定能判定的检查，避免把合法流程误判为错误。

### 3.5 可视化：流程图由代码生成

`to_mermaid(direction="TD")` 导出 Mermaid，并给**条件边**加虚线样式
（`linkStyle ... stroke-dasharray: 5 5`），一眼区分「必走」与「视情况走」；
标签里的裸引号和尖括号会被 `_escape()` 替换，避免破坏 Mermaid 语法。
`to_ascii()` 是终端视图，同时显示每个节点的 `reads` / `writes` / 重试次数 / 错误恢复目标。
`run.py graph --format mermaid` 打印流程图，而 Markdown 报告第二章**内嵌** Mermaid——
报告生成那一刻的拓扑就是真实拓扑，文档不会和实现脱节。

---

## 四、执行引擎

### 4.1 一个 step 的生命周期

`WorkflowEngine.run()` 是 `while run.steps < limit` 循环，每轮七件事：

```
1. 选中当前节点（开始时是 workflow.entry）
2. 发 node_start 事件
3. 执行 node.run(ctx)
     失败 → 重试（最多 node.retries 次，退避 backoff_base ** n）
     仍失败 → 走 node.on_error 恢复分支，或终止流程
4. 把 NodeResult.artifacts 写回共享状态（引擎统一记账，每写一个键发一条 state_change）
5. 把节点期间新发出的消息逐条转成 message 事件
6. 发 node_end 事件（附耗时与产物键名）
7. 按优先级评估出边 → 走第一条成立的边（发 edge_taken，带标签）
     没有边成立 / 走到 END → 流程结束
```

第 4、5 步靠**前后切片**实现：`_apply()` 记录进入节点前的 `len(self.bus)`，
执行完后把 `bus.history()[messages_before:]` 转成事件——节点不需要自己上报「我发了消息」。

### 4.2 顺序、分支、循环、并行

| 流程控制 | 实现方式 | 本项目里的例子 |
| --- | --- | --- |
| **顺序** | 无条件边（`condition=None`） | `START → coder → reviewer` |
| **分支** | 多条带条件的出边 + `priority` 排序 | reviewer 的 8 条出边 |
| **循环** | 一条指回上游节点的边 | `fixer → reviewer`（复审） |
| **并行** | `ParallelNode`：`ThreadPoolExecutor` 并发跑子节点再汇合 | `metrics-parallel` 的 `analyze` 节点（三路指标并行） |

### 4.3 三道保护

1. **步数上限**（`EngineConfig.max_steps` 默认 50；CLI `--max-steps` 默认 40）——循环流程必须有人喊停，
   否则两个互相指向的节点能永远跑下去。触发时发 `loop_guard` 事件、`run.ok = False`、
   原因写「达到步数上限 N（已触发循环保护）」。`run(max_steps=...)` 可单次覆盖（测试常用）。
2. **最大轮次由流程自己表达**——引擎**不硬编码任何业务规则**。「最多修几轮」由 `max_rounds`
   状态键 + `stop_reason` 的 `rounds` 分支决定；引擎只提供 `WorkflowState.increment()` 这类工具。
3. **失败不静默**——节点重试到头仍失败时，**一定**发 `node_error` 事件，并把
   `{node, step, error, attempts}` 记进 `WorkflowRun.failures`，报告与 `to_dict()` 里都能查到。

### 4.4 错误恢复：on_error 边

节点可声明 `on_error="某个节点名"`；失败（重试耗尽）后引擎不终止流程，而是走这条恢复边：

```python
if failure is not None:
    ok = False
    if node.on_error:                       # 错误恢复：转去兜底节点
        current = node.on_error; continue
    reason = f"节点 `{node.name}` 失败：{failure}"; break
```

`on_error` 目标会在 `validate()` 阶段检查是否存在。`EngineConfig.stop_on_failure=False`
可关掉「失败即停」，让引擎在节点失败后继续评估出边（由流程自己决定怎么处理）。

### 4.5 重试与退避

`Node(retries=N)` 声明失败后最多重试几次（默认 0 = 不重试）。引擎在 `_execute()` 里循环
`attempts = node.retries + 1` 次，第 `attempt` 次失败后等待 `backoff_base ** attempt` 秒
（`EngineConfig.backoff_base` 默认 **0.0**，即不等待——演示场景不需要真的 sleep，测试也不用打桩）。
重试发 `node_retry` 事件，带 `attempt` / `error` / `delay` / `retryable` 四个字段。

> **一处如实说明**：`retryable` 只**记录在事件里**（`isinstance(exc, NodeRetryableError)`），
> **不参与**「要不要重试」的判断——只要 `retries > 0`，任何异常都会重试。这是刻意的简化
> （把「重试几次」的决策权完全交给节点声明），但排查时会看到「重试了却没救回来」的记录，
> 所以事件里必须标出异常类型。

### 4.6 并行执行：放在第二条流程里，而不是硬塞进审查流程

并行能力由 `ParallelNode` 提供，但它**没有**出现在 `review-loop` 里——因为「编码 → 审查 → 修复」
本质是串行依赖的：审查必须等编码写完，修复必须等审查给出报告。把并行硬塞进去才是为了演示而演示。

它的真实用例是第二条流程 `metrics-parallel`（`flows/metrics_flow.py`）：

```
开始 ─▶ 扫描文件 ─┬─▶ 统计行数    ─┬─▶ 汇总 Markdown 报告 ─▶ 结束
                  ├─▶ 度量复杂度   ─┤
                  └─▶ 扫描 TODO    ─┘     三个子节点并行，输入互不依赖
```

行数统计、复杂度度量、待办扫描三者读的是同一份文件清单（`files`），各自写不同的状态键
（`lines_stats` / `complexity_stats` / `todo_stats`），因此并行是**顺理成章**的选择，
也正是 `ParallelNode` 构造时那条「子节点 `writes` 不允许重叠」校验要保护的前提。

这条流程还顺带证明了另外两件事：其一，新增流程**只写了一个文件**，`workflow/` 引擎一行未改
（第八章）；其二，它**不靠消息协作**——`tests/test_metrics_flow.py::test_no_new_messages_are_needed`
断言整条流程跑完消息总线里 0 条消息，纯靠共享状态流转。这恰好说明黑板与消息总线各有所长：
需要「谁指挥谁」的因果链时用消息，只需要传产物时用状态就够了。

---

## 五、Agent 间通信与状态管理

作业要求「实现 Agent 间的消息传递**或**状态共享」。本项目**两者都做**，且分工明确：
`WorkflowState`（黑板）负责**产物**（代码、审查报告、修复记录）——大块、需被多方读取、需版本；
`MessageBus`（收件箱）负责**意图**（谁让谁做什么、做完了通知谁）——小块、有指向性、需因果链。

**为什么不能只做一种**：只做黑板会丢失「协作意图」——复盘时只看到 `state[code]` 变了，
却说不清是**谁在指挥谁**，报告的「Agent 协作记录」一节无从写起；只做消息则每次都要把整份代码
塞进消息里，消息总线会变成大对象搬运工。两者结合后，报告里既有
`reviewer → fixer «review_done» 第 1 轮审查：15 条问题…` 这样的因果链，
也有 `state[review] ← reviewer v1` 这样可追溯的产物版本。

需要哪一种取决于流程本身：`review-loop` 两者都用（有明确的「谁指挥谁」），
而 `metrics-parallel` **纯靠状态**——`tests/test_metrics_flow.py::test_no_new_messages_are_needed`
断言它跑完消息总线里 0 条消息（见 4.6）。

### 5.1 消息是刻意「不异步」的

`MessageBus` 只有发送、定向接收、广播、全量存档，**没有异步投递、没有订阅回调**。
理由写在 `message.py` 注释里：本项目节点顺序执行，**异步会让「谁先谁后」变得不可复现**，
日志无法回放、测试无法断言。需要并发时由 `ParallelNode` 负责，消息总线依然先发先存。
消息本身是 `frozen=True` 的不可变数据类，每条带自增 `seq` 与时间戳，可安全存档、回放、断言；
`BROADCAST = "*"` 表示广播，`inbox(agent, since=N)` 支持「只看新消息」。

### 5.2 状态：版本化与审计

`WorkflowState` 不是普通字典，`set()` 里同时做三件事：

```python
def set(self, key, value, *, by="?", note="") -> StateChange:
    self._data[key] = value
    versions = self._history.setdefault(key, [])
    versions.append(value)                       # ① 保留历史版本
    change = StateChange(step=self.step, key=key, by=by,
                         note=note, version=len(versions), size=_describe(value))
    self._changes.append(change)                 # ② 追加审计记录
    return change
```

`StateChange` 记录**七元组**：`step` / `key` / `by` / `note` / `version` / `size` / `at`，
回答「这个值是谁、在第几步写下的、写的是第几版、有多大」。

**为什么必须有版本？** 因为 `code` 会被写两次以上（coder 写初稿、fixer 每轮写回），
`fix_log` 每轮都会被覆盖；没有版本能力，报告就只剩「最后一轮是什么样」。
这一点在本项目里救过两次场，都是真实的报告缺陷：其一，修复明细的累计值原来直接读
`state.get("fix_log")`，而它每轮被覆盖，于是「累计修了几处」永远只显示最后一轮的数字，
修法是从历史版本求和（`state.versions("fix_log")`）；其二，「需人工处理」清单原来用最后一轮
**修复**时的快照，它比最终**审查**早一轮，会漏掉复审新发现的问题，修法是直接从最终
`state["findings"]` 推导。两处都在 `workflow/report.py` 注释里留了说明。

这正是版本化的价值：它让「跨轮次统计」从「不可能」变成「一行 `versions()` 调用」。
另外 `state.snapshot(max_text=200)` 会把长文本截成摘要、列表只留长度，
保证 `WorkflowRun.to_dict()` 的 JSON 不会因为塞进整份代码而变成几 MB。

### 5.3 changes_by 与 changes_of_key：为什么拆成两个名字

`by` 是**写入者**（节点名），`key` 是**状态键**，两者都是字符串。
若合成一个 `changes(filter)`，调用方很容易把「修复 Agent 改过哪些状态」和
「`code` 这个产物被谁改过」写混——而这是两件完全不同的事：
`state.changes_by("fixer")` 按**写入者**筛选（fixer 改过哪些键），
`state.changes_of_key("code")` 按**状态键**筛选（`code` 被谁改过几次）。
所以刻意拆成两个名字明确的方法，并在 docstring 里互相指路；
`tests/test_state.py::test_changes_by_and_of_key_are_different_filters` 专门断言二者结果不同。

---

## 六、三个 Agent

三个 Agent 都继承 `workflow/agents/base.py::Agent`，每个只声明三件事：
**我是谁**（`name` / `role`）、**我读什么**（`reads`）、**我写什么**（`writes`），
然后实现 `act(ctx) -> NodeResult`。`reads`/`writes` 既是文档，也会被 `run.py agents` 打印出来。
三者共享同一个 `AgentRuntime`（`config` + `llm` + `workspace`），由 `AgentRuntime.create()` 构建，
内部调用项目1 的 `Config.from_env()` 与 `create_llm()`——所以**项目1 里配好的 `CR_API_KEY`
在本项目里直接生效**，离线 Mock 降级规则也完全一致。

| Agent | `name` | 读取状态 | 写入状态 | 职责 |
| --- | --- | --- | --- | --- |
| `CoderAgent` | `coder` | `spec`, `workspace` | `code`, `code_path`, `code_history`, `module_name`, `round` | 把需求变成可审查的 Python 初稿，落盘并通知 reviewer |
| `ReviewerAgent` | `reviewer` | `code_path`, `workspace` | `review`, `findings`, `review_history` | 调用项目1 的审查能力，产出结构化报告 |
| `FixerAgent` | `fixer` | `code_path`, `findings`, `round`, `workspace`, `fix_budget` | `code`, `fix_log`, `manual_items`, `round`, `code_history` | 按规则确定性修复并校验语法 |

**编码 Agent 的两种模式**：真实 LLM 模式下 `CODER_SYSTEM_PROMPT` 要求「只输出代码、标准库、有 docstring、
异常要具体、不要硬编码密钥」，回来的文本过一遍 `strip_code_fence()`，空回复直接抛错；
离线 Mock 模式按需求关键词从三个内置模板选一份（`RECORD_EXPORTER` / `CONFIG_READER` / `GENERIC`）。
模板里的问题是**刻意保留的**：硬编码 token、可变默认参数、裸 `except`、`== None`、顺手写的 `eval`、
`range(len(x))`……它们正好是「审查 → 修复」环节的输入；没有这些瑕疵，流程演示就退化成空转。

> **诚实说明**：Mock 模式**不会真的理解需求**，只是按关键词选模板，换一个需求描述大概率还是同一份初稿。
> 真正的编码能力只在 LLM 模式下具备。这一点写在 `coder.py` 的模块 docstring 里。

**审查 Agent 是「从项目1复用」的落点**。它**不重写任何审查逻辑**，只在 `__init__` 里构造项目1 的
`ReviewAgent`，并在 `act()` 里调一次 `review([rel_path])`：`self._review_agent = ReviewAgent(
self.runtime.config, registry=build_default_registry(self.runtime.config))`，
然后 `result = self._review_agent.review([rel_path])`，取 `result.report`。

收益非常直观：项目1 花大力气做的 **26 条 AST 规则、路径沙箱、上下文压缩、降级交付**，
在本项目里一行都没重写就直接生效；连离线 Mock 降级、重试退避的行为也完全一致。
`act()` 里还有两处细节：`round_no` 从状态读（默认 1）并写进 `review_history`，
形成「第几轮审查得多少分」的演化表；通过 `ctx.send("fixer", "review_done", ...)`
把「发现多少问题、其中几条可自动修」明确交给 fixer——这是消息总线上最主要的协作信号。

---

## 七、确定性修复规则

### 7.1 为什么不全交给大模型

审查报告里每条问题都带**规则名 + 行号**，其中相当一部分改法是**确定的**：
`== None` 就改成 `is None`，裸 `except` 就改成 `except Exception`。用规则改比让模型重写整个文件可靠得多：
改完能 **diff**、能回归、能解释「为什么这么改」；不会「顺手把无关代码也改了」
（这是 LLM 重写整文件时最常见也最难 review 的问题）；不消耗 token、**离线可跑**。
模型只在规则覆盖不到的地方兜底（本项目的实现里，这类问题直接进人工清单，不做自动改写）。

### 7.2 能自动修的 7 条 vs 需人工的 17 条

`FIXERS`（`workflow/agents/repairs.py`）登记 7 条可自动修复的规则：

| 规则 | 改动方式 |
| --- | --- |
| `none-comparison` | `== None` → `is None`（`!= None` → `is not None`），用负向断言避免误伤 `<=` / `>=` |
| `bare-except` | 裸 `except:` → `except Exception:`；若异常体是 `pass`，顺手换成 `logger.warning(...)` 并补 `logger` |
| `except-pass` | `except ...: pass` → 记录日志 |
| `unused-import` | 删除整条导入；`from x import a, b` 只摘掉不用的那个名字 |
| `mutable-default` | 可变默认参数 → `None` 哨兵 + 函数体内初始化 |
| `hardcoded-secret` | 硬编码字符串 → `os.environ.get("名字大写", "")`，并确保 `import os` |
| `missing-docstring` | 在函数体开头插入一行占位 docstring |

`MANUAL_RULES` 登记 17 条**已知无法机械修复**的规则，每条写明理由：`high-complexity` / `long-function`
（要重排控制流）、`eval-exec`（取决于业务语义）、`os-system` / `subprocess-shell`（要确认参数边界）、
`pickle-loads` / `yaml-load-unsafe`（改变数据兼容性）、`redefined-function` / `star-import`
（要确认哪个定义才是期望的）、`mutable-class-attr` / `global-statement`（要重构状态传递）、
`open-without-with` / `broad-except`（要确认语义）、`lambda-assignment` / `range-len` / `long-line`
（收益低于风险）、`todo-comment`（是待办事项，不是代码缺陷）。

> **口径说明**：项目1 的规则引擎共 26 条规则，7 + 17 = 24，剩下 `syntax-error` 与 `too-many-args`
> 既不在 `FIXERS` 也不在 `MANUAL_RULES`，会走默认说明「目前没有对应的自动修复规则，需要人工判断」——
> 实测报告里 `参数过多（7 个）` 那一行就是这么来的。
> `tests/test_repairs.py::RegistryTests` 用 4 个用例守住这张表的一致性：每个 fixer 都要有文档、
> 每条人工规则都要有理由、**同一条规则不允许同时出现在两边**。

### 7.3 四条底线

1. **只改报告点到的地方**——规则按 `规则名 + 行号` 定位，不做「顺手重构」。
   `SourceEditor` 还强制**同一行只允许一次 replace/delete**（后到的被拒并记录原因），
   但允许「替换该行 + 在其后插入若干行」——「改函数签名 + 补参数校验」就是这个模式。
2. **改完必须能编译**——渲染后的源码先过一遍 `ast.parse`（`self._syntax_error(new_code)`），
   语法不过就**整轮丢弃**并写进 `fix_log.rolled_back`，让 `stop_reason` 返回 `rollback` 结束流程。
   宁可这一轮白干，也不能把好代码改成坏代码。
3. **修不了就说出来**——无法机械修复的规则进入 `manual_items` 清单，由报告第四节原样呈现
   （带「为什么不能自动修」）。「修不了」和「没看见」是两回事。
4. **无进展就停**——修复 Agent 统计本轮实际改了几处；一处都没改时 `stop_reason` 返回 `stall`，流程结束。
   **这是防止两个 Agent 互相空转的关键**：没有进展就必须停，而不是耗光最大轮次。

### 7.4 两个真实踩过的坑（回归测试守着）

两个坑都属于「修复把代码改坏」这一类，`tests/test_repairs.py::RegressionTests` 专门守着它们。

**坑一：多行函数签名被插坏 → `find_statement_end()`**

`mutable-default` 的修法是「把签名里的 `[]` 改成 `None`，再在函数体开头插入守则」。
「函数体开头」一开始天真地取成 `def` 行的下一行。但签名经常写成多行：

```python
def load_records(path, options={}, limit=None, strict=False,
                 verbose=False, dry_run=False, encoding="utf-8"):
```

此时往 `def` 行的下一行插守则，会把代码**插进参数列表中间**，直接产生语法错误。
修法是 `SourceEditor.find_statement_end(start)`：从起始行往下走，按**括号配平**
（`(` `[` `{` 计数加减）加上「行尾是冒号」判断语句真正的结束行。字符串与注释里的括号不计入——
它复用了项目1 的 `mask_strings_and_comments()`，所以 `f("(")` 这类写法不会被误判。

**坑二：守则插到 docstring 前面，把 docstring 降级成普通语句 → `body_anchor()`**

修好坑一之后，守则插在了签名结束行后面。但很多函数第一行是 docstring：

```python
def load_records(path, options=None):
    """读取 CSV 文件并返回记录列表。"""
    if options is None:      # ← 守则应该插在这里
        options = {}
```

若守则插在 docstring **前面**，那个字符串就成了一条无用的表达式语句——`__doc__` 变成 `None`，
函数失去文档。更糟的是：**下一轮审查会因此报出「缺少文档字符串」**，
于是流程开始修一个自己刚制造出来的问题。修法是 `SourceEditor.body_anchor(signature_end)`：
从签名结束行往下**跳过空行**，若第一个非空行以 `"""` 或 `'''` 开头，就返回 docstring 的**结束行**
（处理单行与多行两种情况）。`mutable-default` 的插入点最终是
`anchor = editor.body_anchor(editor.find_statement_end(finding.line))`，再 `insert_after(anchor, guards)`。

`RegressionTests` 的三个用例分别守着：多行签名不插错、docstring 修复后仍是第一条语句、
`== None` 替换不产生多余空格。

---

## 八、可扩展性

### 8.1 新增一个 Agent：只改流程定义

假设要加一个「安全专项审查 Agent」。**需要改的**：新建 `workflow/agents/security.py`，
继承 `Agent`，声明 `name` / `role` / `reads` / `writes`，实现 `act(ctx)`；
然后在 `flows/review_loop.py` 里 `workflow.add(AgentNode(SecurityAgent(runtime)))` 并连两条边。

**不需要改的**：`engine.py`、`graph.py`、`state.py`、`message.py`、`events.py`、`report.py`、`cli.py`
——全部零改动。因为引擎不认识业务概念：报告自动多出一行「节点执行 security×1」，
流程图自动多出一个节点，消息流水里自动出现这个新 Agent 收发消息。

### 8.2 新增一条流程：只加一个文件

在 `flows/` 下新建文件，用 `Workflow` + `connect`/`branch` 组装已有节点，
再往 `FLOWS`（`flows/__init__.py` 的字典）里加一行即可被 `--flow` 使用：

```python
FLOWS = {
    "review-loop": build_review_workflow,
    "metrics-parallel": build_metrics_workflow,   # 新增流程只加了这一行
}
```

这不是设想——`metrics-parallel` 就是这么加进来的：`build_metrics_workflow(runtime, **_ignored)`
用 `**_ignored` 吞掉 CLI 统一传入的 `max_rounds` / `target_score`（这条流程没有修复循环，用不到），
其余全靠 `workflow/` 里已有的节点类型组装。`workflow/cli.py::_load_flow` 就是查这张表，
查不到才报 `WorkflowError`；`tests/test_cli.py::test_unknown_flow_returns_usage_error` 覆盖了这条错误路径。

### 8.3 替换某个节点的实现：接口不变即可

想「把确定性修复换成 LLM 修复」，只需写一个新的 `FixerAgent` 子类
（`name` 保持 `fixer`，`writes` 保持同一组键），在流程定义里换掉那个节点——
**其他 Agent、引擎、报告都不用动**。三个 Agent 之间没有任何直接引用，
只共享 `AgentRuntime` 与 `NodeContext` 这两个接口。

### 8.4 `--fix-budget` 的设计理由

`fix_budget`（状态键，CLI `--fix-budget`，默认 0 = 不限）限制**每轮最多自动改几处**；
超过上限的修复不执行，记进 `deferred` 并**顺延到下一轮**。两个作用：
其一，**工程上合理**——真实项目里一次改几十处代码没法 review，增量重构才是常规做法；
其二，**让「多轮循环」可被验证**，不是靠人为拖延，而是靠一个有意义的约束。实测（`--fix-budget 3`）：

| 轮次 | 本轮改动 | 顺延到下一轮 |
| ---: | ---: | ---: |
| 第 1 轮 | 3 | 6 |
| 第 2 轮 | 3 | 3 |
| 第 3 轮 | 2 | 0 |

`tests/test_flow.py` 有两个用例守这条：`test_fix_budget_forces_multiple_rounds`
断言「设了 budget 确实产生多轮」，`test_budgeted_run_still_converges_to_same_quality`
断言「分轮改完的最终质量与不限速时一样」——即预算只影响节奏，不影响结果。

---

## 九、测试策略

```bash
python -m unittest discover -s tests -t .     # 143 个用例，约 1.5 秒，零依赖
```

| 测试文件 | 用例数 | 覆盖内容 |
| --- | ---: | --- |
| `test_state.py` | 19 | 状态读写与 `require` 报错、版本化、审计筛选、消息总线（定向/广播/序号/不可变） |
| `test_graph.py` | 24 | 流程定义、6 类硬错误校验、软警告、Mermaid/ASCII 渲染、`ParallelNode` 竞态拦截 |
| `test_engine.py` | 20 | 顺序执行、条件分支优先级、循环与步数熔断、重试、`on_error` 恢复、事件可观测性、并行集成 |
| `test_repairs.py` | 31 | `SourceEditor` 原子操作、7 条规则逐条验证、**3 个回归测试**、规则表一致性 |
| `test_flow.py` | 28 | 端到端跑通、跨轮质量提升、轮次上限、`fix_budget` 多轮、8 种停止原因、协作与复用断言、报告章节 |
| `test_metrics_flow.py` | 9 | 第二条流程：并行产物汇合、度量准确性、扫描范围排除产物目录、零消息协作 |
| `test_cli.py` | 12 | 四个子命令、`--json`、自定义 `--spec`、`--spec-file`、错误退出码 |

两处关键手法：**引擎测试不用 LLM**——`test_engine.py` 用「把执行次数记进状态」的 `FunctionNode`
构造流程，因此循环、分支、重试、错误恢复可被精确断言且毫秒级完成；
**端到端测试用离线 Mock**——`test_flow.py::FlowTestCase` 在临时目录里构造 `AgentRuntime`，
跑真实的三个 Agent（编码走模板、审查走 AST 引擎、修复走规则），断言「三个 Agent 都被访问过」
「生成的代码能被 `ast.parse` 解析」「评分跨轮提升」「迭代不超过 `max_rounds`」，整条流程不联网。

**回归测试**（两个真实 Bug，见 7.4）：`test_guards_not_inserted_into_multiline_signature`、
`test_docstring_stays_first_after_mutable_default_fix`、`test_none_comparison_does_not_double_space`。

---

## 十、与项目1 的关系

### 10.1 复用了什么

| 复用内容 | 方式 | 在本项目里的作用 |
| --- | --- | --- |
| `codereview.agent.ReviewAgent` | 整体包装成 `ReviewerAgent` | 审查节点的全部能力来源，**零重写** |
| `codereview.tools.python_ast.PythonAnalyzer` | `metrics-parallel` 直接实例化调用 | 度量复杂度（函数级 `complexity`、总数、>10 的个数、最复杂的三个函数） |
| 26 条 AST 规则（`tools/python_ast.py`） | 经由 `build_default_registry()` 间接使用 | 问题清单、行号、严重度、修复建议 |
| `_render_within_budget()` | 随 vendored 包一起生效 | 观察预算内保证输出始终是合法 JSON，问题清单不会因截断而整段丢失 |
| `mask_strings_and_comments()` | 直接调用 | `find_statement_end()` 判断括号配平时屏蔽字符串与注释 |
| `Config.from_env()` / `create_llm()` | 由 `AgentRuntime.create()` 调用 | 环境变量、`.env`、离线 Mock 降级与项目1 完全一致 |
| `enable_utf8_output()` / `make_style()` | CLI 直接调用 | Windows 中文输出与终端着色 |

### 10.2 如何 vendored

`codereview/` 是项目1 的**完整拷贝**（17 个文件），放在本项目根目录下；`run.py` 把仓库根目录加入
`sys.path`（`sys.path.insert(0, str(Path(__file__).resolve().parent))`），随后
`from workflow.cli import main`。于是 `codereview` 与 `workflow` 都是顶层可导入包，
`pyproject.toml` 里 `packages = ["workflow", "workflow.agents", "flows", "codereview", "codereview.tools"]`
也把两者一起打包。**保证项目2 可独立运行**：不依赖项目1 的安装、不依赖项目1 的路径、
不依赖任何第三方库（`dependencies = []`）。clone → `python run.py run` 即可跑通全流程。

### 10.3 两个项目的分工

项目1（`code-review-agent`）是**单个 Agent**（ReAct 循环），解决「怎么让一个 Agent 把代码审好」，
核心机制是工具调用、上下文压缩、降级交付；本项目是**多 Agent 编排**（Workflow），
解决「怎么让几个 Agent 协作完成一件事」，核心机制是节点与条件边、共享状态、消息总线、执行引擎。
两者是「能力提供方」与「编排方」的关系，都刻意保持零第三方依赖、都提供离线 Mock 模式、
报告风格一致（表格 + 分节 + 附录）——因为这两个项目会一起提交，读者不该在两份报告之间切换阅读习惯。

---

## 附：已知局限

1. **Mock 模式的编码 Agent 不理解需求**——按关键词选模板，换个需求很可能还是同一份初稿。
2. **重试不区分异常类型**——`NodeRetryableError` 只记录在事件里，不参与是否重试的判断（4.5 节）。
3. **`retries` / `on_error` 在当前流程里没有配置**——两个机制都已实现并被单元测试覆盖
   （`test_retry_until_success`、`test_on_error_edge_recovers`），但 `review-loop` 的三个节点
   用的都是默认值（`retries=0`、无 `on_error`），因为它们目前不会失败到需要恢复。
4. **修复规则只覆盖 7 条**——复杂重构类问题（复杂度、长函数）明确不做机械替换；
   `syntax-error` 没有对应的自动修复，因为本项目修复流程建立在「代码至少能解析」的前提上。
5. **状态快照会截断长文本**（默认 200 字符）——`to_dict()` 的 JSON 里读不到完整代码，
   完整代码要看 `runs/<run>/solution.py` 或报告第六章。
6. **没有持久化**——状态与消息都在内存里，进程结束即消失（MVP 范围）；
   Web 编排界面未做（作业标为可选），可视化通过 Mermaid 导出与终端 ASCII 流程图提供。
