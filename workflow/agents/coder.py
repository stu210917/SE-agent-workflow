"""编码 Agent：把自然语言需求变成一份可审查的 Python 初稿。

两种模式
--------
* **真实 LLM 模式**：把规格交给模型，让它写代码；
* **离线 Mock 模式**：从内置模板库按关键词选一份「朴素初稿」。

Mock 模板里的问题是**刻意保留**的：初学者常见的硬编码凭据、可变默认参数、
裸 ``except``、``== None``、顺手的 ``eval``……它们正好是本项目要演示的
「审查 → 修复」环节的输入。没有这些瑕疵，流程演示就退化成空转。

> 诚实说明：Mock 模式**不会真的理解需求**，它只是按关键词选模板；
> 换一个需求描述大概率还是同一份初稿。真实编码能力只在 LLM 模式下具备。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codereview.utils import strip_code_fence

from ..node import NodeContext, NodeResult
from .base import Agent, append_history

# --------------------------------------------------------------------------- #
# 内置「朴素初稿」模板
# --------------------------------------------------------------------------- #
RECORD_EXPORTER = '''# -*- coding: utf-8 -*-
"""记录导出工具：读取 CSV，过滤后导出为 JSON。"""

import csv
import json
import sys

SECRET_TOKEN = "sk-draft-000111222333"

DEFAULTS = {"encoding": "utf-8"}


def load_records(path, options={}, limit=None, strict=False,
                 verbose=False, dry_run=False, encoding="utf-8"):
    """读取 CSV 文件并返回记录列表。"""
    rows = []
    fh = open(path, encoding=encoding)
    for line in fh:
        rows.append(line.strip().split(","))
    fh.close()

    for i in range(len(rows)):
        if rows[i] == None:
            rows[i] = []

    if limit == None:
        limit = 100

    try:
        if strict:
            rows = [row for row in rows if len(row) > 1]
    except:
        pass

    expression = options.get("filter", "True")
    if eval(expression):
        rows = list(rows)

    return rows[:limit]


def export_json(records, target_path, options={}):
    """把记录写成 JSON 文件。"""
    payload = {"count": len(records), "rows": records}
    if options.get("pretty"):
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(payload, ensure_ascii=False)
    writer = open(target_path, "w", encoding="utf-8")
    writer.write(text)
    writer.close()
    return target_path


def summarize(records, options={}):
    """统计记录条数与字段数。"""
    if len(records) == 0:
        return {"count": 0, "fields": 0}
    fields = max(len(row) for row in records)
    result = {"count": len(records), "fields": fields}
    if options.get("verbose"):
        result["sample"] = records[0]
    return result


def main(argv=None):
    """命令行入口。"""
    argv = argv or sys.argv[1:]
    if not argv:
        print("用法: python solution.py <csv_path>")
        return 1
    records = load_records(argv[0], limit=50)
    print(summarize(records, {"verbose": True}))
    # TODO: 支持从标准输入读取，并补一份使用示例
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

CONFIG_READER = '''# -*- coding: utf-8 -*-
"""配置读取工具：从 JSON 文件加载配置并做合并。"""

import json
import os
import re

API_KEY = "cfg-9876543210abcdef"
_cache = {}


def read_config(path, overrides={}, defaults={}, strict=False, encoding="utf-8"):
    """读取配置并合并默认值与覆盖值。"""
    handler = open(path, encoding=encoding)
    raw = handler.read()
    handler.close()

    data = json.loads(raw)

    for key in defaults:
        if data.get(key) == None:
            data[key] = defaults[key]

    for key in overrides:
        data[key] = overrides[key]

    try:
        if strict:
            validate(data)
    except:
        pass

    pattern = overrides.get("match", ".*")
    if re.match(pattern, str(data)):
        _cache[path] = data
    return data


def validate(data):
    """校验必填字段。"""
    required = ["name", "version"]
    for field in required:
        if data.get(field) == None:
            raise ValueError("缺少字段: " + field)
    return True


def merge(base, extra, mode="deep"):
    """合并两份配置。"""
    if mode == "shallow":
        result = dict(base)
        result.update(extra)
        return result
    result = dict(base)
    for key in extra:
        if key in result and isinstance(result[key], dict) and isinstance(extra[key], dict):
            result[key] = merge(result[key], extra[key], mode)
        else:
            result[key] = extra[key]
    return result


def main():
    """命令行入口。"""
    import sys
    if len(sys.argv) < 2:
        print("用法: python solution.py <config.json>")
        return 1
    print(read_config(sys.argv[1]))
    # FIXME: 需要支持环境变量覆盖与 .env 文件
    return 0


if __name__ == "__main__":
    main()
'''

GENERIC = '''# -*- coding: utf-8 -*-
"""任务实现（编码 Agent 的初稿）。"""

import json
import os
import sys

ACCESS_TOKEN = "draft-token-abcdef123456"


def process(items=[], options={}, limit=None, verbose=False, strict=False):
    """处理输入数据并返回结果。"""
    result = []
    for i in range(len(items)):
        if items[i] == None:
            continue
        try:
            result.append(items[i])
        except:
            pass

    if limit == None:
        limit = len(result)

    if verbose:
        print(json.dumps({"count": len(result)}, ensure_ascii=False))
    return result[:limit]


def save(result, path, options={}):
    """把结果写入文件。"""
    handler = open(path, "w", encoding="utf-8")
    handler.write(json.dumps(result, ensure_ascii=False))
    handler.close()
    return path


def main():
    """命令行入口。"""
    print(save(process([1, 2, 3]), "out.json"))
    # TODO: 补充参数解析与错误处理
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

#: 关键词 → 模板（按顺序匹配，命中即用）
TEMPLATES: list[tuple[tuple[str, ...], str, str]] = [
    (("csv", "导出", "export", "json"), RECORD_EXPORTER, "记录导出工具"),
    (("配置", "config", "读取", "加载"), CONFIG_READER, "配置读取工具"),
]

CODER_SYSTEM_PROMPT = """你是一名资深 Python 工程师，负责根据需求编写一个**完整、可直接运行**的 Python 模块。

要求：
1. 只输出代码本身，不要任何解释文字，不要用 Markdown 代码块包裹；
2. 使用标准库，模块顶层可直接 `python 文件名.py` 运行；
3. 包含模块文档字符串；公共函数要有 docstring；
4. 做好错误处理，异常要具体，不要用裸 except；
5. 不要硬编码任何密钥、口令或 token。
"""


class CoderAgent(Agent):
    """编码 Agent：产出第一版实现。"""

    name = "coder"
    role = "编码 Agent：把需求写成可运行的第一版代码"
    reads = ("spec", "workspace")
    writes = ("code", "code_path", "code_history", "module_name", "round")

    def act(self, ctx: NodeContext) -> NodeResult:
        spec = str(ctx.state.require("spec", str)).strip()
        workspace = Path(ctx.state.require("workspace"))
        module_name = str(ctx.state.get("module_name") or "solution")
        target = workspace / f"{module_name}.py"

        if self.runtime.mock:
            code, template_name = self._from_template(spec)
            source = f"离线 Mock 模板（{template_name}）"
        else:
            code = self._from_llm(spec, module_name)
            source = f"LLM（{self.runtime.config.model}）"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        rel_path = self._relative(target, workspace)

        history = append_history(ctx, "code_history", {
            "round": 1, "by": self.name, "reason": "初稿", "source": source,
            "path": rel_path, "chars": len(code),
        })
        ctx.send("reviewer", "code_ready", summary=f"{rel_path} 已就绪（{len(code)} 字符，来源：{source}）",
                 path=rel_path, chars=len(code))
        ctx.log(f"已生成 {rel_path}（{source}）")

        return NodeResult(
            summary=f"生成初稿 {rel_path}（{len(code)} 字符，来源：{source}）",
            artifacts={
                "code": code,
                "code_path": rel_path,
                "code_history": history,
                "module_name": module_name,
                "round": 1,
            },
            detail={"source": source},
        )

    # ------------------------------------------------------------------ #
    def _from_template(self, spec: str) -> tuple[str, str]:
        """按需求里的关键词挑模板；都不命中时用通用模板。"""
        lowered = spec.lower()
        for keywords, template, label in TEMPLATES:
            if any(keyword in lowered for keyword in keywords):
                return template, label
        return GENERIC, "通用模板"

    def _from_llm(self, spec: str, module_name: str) -> str:
        """真实模式：让模型写代码，并做最基本的产出校验。"""
        prompt = (
            f"需求：\n{spec}\n\n"
            f"请实现为单个模块 `{module_name}.py`。"
        )
        raw = self.runtime.complete(CODER_SYSTEM_PROMPT, prompt)
        code = strip_code_fence(raw)
        if not code.strip():
            raise ValueError("编码 Agent 拿到了空回复，无法生成代码")
        return code if code.endswith("\n") else code + "\n"

    @staticmethod
    def _relative(path: Path, workspace: Path) -> str:
        try:
            return path.relative_to(workspace).as_posix()
        except ValueError:
            return str(path)


__all__ = ["CoderAgent", "TEMPLATES", "RECORD_EXPORTER", "CONFIG_READER", "GENERIC"]
