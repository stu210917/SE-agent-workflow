"""确定性修复规则：把审查报告里的问题条目变成真正的代码改动。

**为什么修复 Agent 不全交给大模型？**
审查报告里的每一条问题都带规则名与行号，其中相当一部分（`== None`、
裸 `except`、未使用的导入、可变默认参数……）的改法是**确定的**，
用规则改比让模型重写整个文件更可靠：改完能 diff、能跑测试、
也不会「顺手把无关代码也改了」。模型只在规则覆盖不到的地方兜底。

**不能自动修的必须说出来。** 复杂度高、`eval` 执行动态代码这类问题
需要理解业务语义，机械替换只会引入新 Bug。这类规则统一登记在
:data:`MANUAL_RULES` 里，修复 Agent 会把它们原样列进「需人工处理」清单——
「修不了」和「没看见」是两回事，报告必须诚实区分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from codereview.utils import mask_strings_and_comments

#: 已知无法机械修复的规则 → 原因（会出现在报告的「需人工处理」清单里）
MANUAL_RULES: dict[str, str] = {
    "high-complexity": "需要拆分函数结构，涉及控制流重排，机械替换风险过高",
    "long-function": "需要按职责拆分，属于设计层面的改动",
    "eval-exec": "取决于业务语义（是求值表达式还是解析配置），无法机械替换",
    "os-system": "需要改写为 subprocess 并确认参数边界",
    "subprocess-shell": "需要确认参数是否含用户输入后再决定改法",
    "pickle-loads": "替换序列化格式会改变数据兼容性",
    "yaml-load-unsafe": "需要确认调用方是否已限制数据来源",
    "redefined-function": "需要确认哪个定义才是期望的，属于语义决策",
    "star-import": "需要逐个确认实际用到了哪些名字",
    "mutable-class-attr": "需要确认该属性应当共享还是每实例独立",
    "global-statement": "需要重构状态传递方式",
    "open-without-with": "需要确认文件生命周期与异常语义",
    "lambda-assignment": "需要展开为 def 并补文档，收益低于改动风险",
    "range-len": "需要确认循环体内是否还用到下标做其他计算",
    "broad-except": "需要确认应当捕获哪些具体异常",
    "long-line": "折行需要人工判断可读性",
    "todo-comment": "属于待办事项，不是代码缺陷",
}


# --------------------------------------------------------------------------- #
# 源码编辑
# --------------------------------------------------------------------------- #
@dataclass
class Edit:
    """一次行级编辑。``kind`` 取 ``replace`` / ``delete`` / ``insert``。"""

    line: int
    kind: str
    payload: list[str] = field(default_factory=list)
    note: str = ""

    def describe(self) -> str:
        if self.kind == "replace":
            return f"第 {self.line} 行替换为：{self.payload[0].strip()[:60]}"
        if self.kind == "delete":
            return f"删除第 {self.line} 行"
        return f"在第 {self.line} 行后插入 {len(self.payload)} 行"


class SourceEditor:
    """收集行级编辑，最后一次渲染成新源码。

    同一行只允许一次 replace/delete（后到的会被拒并记录原因），
    但允许在替换的同时于该行之后插入若干行——「改函数签名 + 补参数校验」
    就是这个模式。
    """

    def __init__(self, text: str) -> None:
        self.original = text
        self.lines = text.splitlines()
        self._replaces: dict[int, Edit] = {}
        self._deletes: dict[int, Edit] = {}
        self._inserts: dict[int, list[str]] = {}
        self.skipped: list[str] = []

    # ------------------------------------------------------------------ #
    def _check(self, line: int, kind: str) -> bool:
        if not 1 <= line <= len(self.lines):
            self.skipped.append(f"第 {line} 行超出文件范围，已跳过")
            return False
        if line in self._replaces or line in self._deletes:
            self.skipped.append(f"第 {line} 行已有一处编辑（{kind} 冲突），已跳过")
            return False
        return True

    def replace(self, line: int, new_text: str, *, note: str = "") -> bool:
        if not self._check(line, "replace"):
            return False
        self._replaces[line] = Edit(line=line, kind="replace", payload=[new_text], note=note)
        return True

    def delete(self, line: int, *, note: str = "") -> bool:
        if not self._check(line, "delete"):
            return False
        self._deletes[line] = Edit(line=line, kind="delete", note=note)
        return True

    def insert_after(self, line: int, new_lines: Sequence[str], *, note: str = "") -> bool:
        """在第 ``line`` 行之后插入若干行；``line=0`` 表示插到文件最前面。"""
        if not 0 <= line <= len(self.lines):
            self.skipped.append(f"第 {line} 行超出文件范围，插入已跳过")
            return False
        self._inserts.setdefault(line, []).extend(new_lines)
        return True

    def line(self, number: int) -> str:
        return self.lines[number - 1] if 1 <= number <= len(self.lines) else ""

    def find_line(self, start: int, predicate: Callable[[str], bool], *, limit: int = 12) -> int | None:
        """从 ``start`` 行起向下找第一个满足条件的行号（用于定位 ``except`` 后面的 ``pass``）。"""
        for number in range(start, min(start + limit, len(self.lines) + 1)):
            if predicate(self.lines[number - 1]):
                return number
        return None

    def find_statement_end(self, start: int, *, limit: int = 15) -> int:
        """找到从 ``start`` 行开始的语句在哪一行结束。

        为什么需要它：函数签名经常写成多行——

        .. code-block:: python

            def f(a, b,
                  c=None):

        此时「在 ``def`` 行之后插入函数体语句」会把代码插进参数列表里，
        直接产生语法错误。这里按括号配平 + 行尾冒号判断真正的签名结束行。
        （字符串与注释里的括号不计入，避免 ``f("(")`` 这类误判。）
        """
        depth = 0
        for number in range(start, min(start + limit, len(self.lines) + 1)):
            raw = self.line(number)
            masked = mask_strings_and_comments(raw)
            depth += masked.count("(") + masked.count("[") + masked.count("{")
            depth -= masked.count(")") + masked.count("]") + masked.count("}")
            if depth <= 0 and raw.rstrip().endswith(":"):
                return number
        return start

    def body_anchor(self, signature_end: int) -> int:
        """返回「往函数体里插新语句」应当锚定的行号。

        若函数已有 docstring，必须插到 docstring **之后**：插在它前面会把
        docstring 挤成一条无用的字符串表达式（``__doc__`` 变成 None），
        紧接着的下一轮审查就会报「缺少文档字符串」——这是本项目真踩过的坑。
        """
        number = signature_end + 1
        while number <= len(self.lines) and not self.line(number).strip():
            number += 1
        if number > len(self.lines):
            return signature_end

        text = self.line(number).strip()
        if not text.startswith(('"""', "'''")):
            return signature_end
        quote = text[:3]
        if len(text) > 3 and text.rstrip().endswith(quote):
            return number  # 单行 docstring
        for candidate in range(number + 1, min(number + 40, len(self.lines) + 1)):
            if quote in self.line(candidate):
                return candidate
        return number

    # ------------------------------------------------------------------ #
    @property
    def edit_count(self) -> int:
        return len(self._replaces) + len(self._deletes) + sum(1 for v in self._inserts.values() if v)

    def render(self) -> str:
        """按行号顺序重建源码（删除与替换先做，插入后做）。"""
        output: list[str] = list(self._inserts.get(0, []))  # line=0 表示插到文件最前面
        for number, text in enumerate(self.lines, start=1):
            if number not in self._deletes:
                output.append(self._replaces[number].payload[0] if number in self._replaces else text)
            output.extend(self._inserts.get(number, []))
        trailing = "\n" if self.original.endswith("\n") else ""
        return "\n".join(output) + trailing


# --------------------------------------------------------------------------- #
# 单个修复规则
# --------------------------------------------------------------------------- #
@dataclass
class FixOutcome:
    """一条修复的结果。"""

    rule: str
    line: int
    applied: bool
    description: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "line": self.line, "applied": self.applied,
                "description": self.description, "reason": self.reason}


#: 修复函数签名：``(editor, finding) -> FixOutcome``
Fixer = Callable[[SourceEditor, Any], FixOutcome]

_NONE_EQ = re.compile(r"(?<![=!<>])\s*==\s*None\b")
_NONE_NE = re.compile(r"\s*!=\s*None\b")
_IMPORT_SINGLE = re.compile(r"^(?P<indent>\s*)import\s+(?P<name>[A-Za-z_][\w.]*)\s*(?:#.*)?$")
_IMPORT_FROM = re.compile(
    r"^(?P<indent>\s*)from\s+(?P<module>[\w.]+)\s+import\s+(?P<names>[^#]+?)\s*(?:#.*)?$"
)
_DEF_LINE = re.compile(r"^(?P<indent>\s*)(?P<kw>async\s+def|def)\s+(?P<name>\w+)\s*\(")


def fix_none_comparison(editor: SourceEditor, finding: Any) -> FixOutcome:
    """``== None`` → ``is None``（反过来同理）。"""
    text = editor.line(finding.line)
    new = _NONE_EQ.sub(" is None", text)
    new = _NONE_NE.sub(" is not None", new)
    if new == text:
        return FixOutcome(finding.rule, finding.line, False, "", "该行未找到可替换的比较写法")
    editor.replace(finding.line, new, note="None 比较改为身份比较")
    return FixOutcome(finding.rule, finding.line, True, "`== None` → `is None`")


def fix_bare_except(editor: SourceEditor, finding: Any) -> FixOutcome:
    """裸 ``except:`` → ``except Exception:``，顺带把紧随其后的 ``pass`` 换成日志。"""
    text = editor.line(finding.line)
    if "except" not in text:
        return FixOutcome(finding.rule, finding.line, False, "", "该行不是 except 语句")
    indent = text[: len(text) - len(text.lstrip())]
    new = re.sub(r"except\s*:", "except Exception:", text, count=1)
    if new == text:
        return FixOutcome(finding.rule, finding.line, False, "", "未匹配到裸 except 写法")
    if not editor.replace(finding.line, new, note="裸 except 改为 except Exception"):
        return FixOutcome(finding.rule, finding.line, False, "", "该行已有其他编辑")

    # 如果异常体是 `pass`，顺手替换成日志——否则「不再吞 KeyboardInterrupt」只做了一半
    pass_line = editor.find_line(finding.line + 1, lambda s: s.strip() == "pass")
    if pass_line is not None:
        body_indent = editor.line(pass_line)[: len(editor.line(pass_line)) - len(editor.line(pass_line).lstrip())]
        if editor.replace(pass_line, f"{body_indent}logger.warning(\"捕获到异常，已忽略\", exc_info=True)",
                          note="异常不再被静默吞掉"):
            editor.insert_after(finding.line, [f"{indent}    # 明确捕获范围，避免连 KeyboardInterrupt 一起吞掉"])
            _ensure_logger(editor)
            return FixOutcome(finding.rule, finding.line, True,
                              "裸 `except:` → `except Exception:`，并用日志替代静默 pass")
    return FixOutcome(finding.rule, finding.line, True, "裸 `except:` → `except Exception:`")


def fix_except_pass(editor: SourceEditor, finding: Any) -> FixOutcome:
    """``except ...: pass`` → 记录日志。"""
    pass_line = editor.find_line(finding.line, lambda s: s.strip() == "pass", limit=4)
    if pass_line is None:
        return FixOutcome(finding.rule, finding.line, False, "", "未在该 except 块中找到 pass")
    indent = editor.line(pass_line)[: len(editor.line(pass_line)) - len(editor.line(pass_line).lstrip())]
    if not editor.replace(pass_line, f"{indent}logger.warning(\"捕获到异常\", exc_info=True)", note="异常改为记录日志"):
        return FixOutcome(finding.rule, finding.line, False, "", "该行已有其他编辑")
    _ensure_logger(editor)
    return FixOutcome(finding.rule, finding.line, True, "`except: pass` → 记录日志，异常不再被吞掉")


def fix_unused_import(editor: SourceEditor, finding: Any) -> FixOutcome:
    """删除未使用的导入；``from x import a, b`` 只摘掉不用的那个名字。"""
    text = editor.line(finding.line)
    name_match = re.search(r"`([^`]+)`", finding.title or "")
    unused = name_match.group(1) if name_match else ""

    single = _IMPORT_SINGLE.match(text)
    if single and (not unused or single.group("name").split(".")[0] == unused):
        if editor.delete(finding.line, note=f"删除未使用的导入 {single.group('name')}"):
            return FixOutcome(finding.rule, finding.line, True, f"删除未使用的导入 `{single.group('name')}`")
        return FixOutcome(finding.rule, finding.line, False, "", "该行已有其他编辑")

    multi = _IMPORT_FROM.match(text)
    if multi and unused:
        names = [chunk.strip() for chunk in multi.group("names").split(",") if chunk.strip()]
        kept = [chunk for chunk in names if chunk.split(" as ")[0].strip() != unused]
        if len(kept) == len(names):
            return FixOutcome(finding.rule, finding.line, False, "", f"该行没有找到名字 `{unused}`")
        if not kept:
            if editor.delete(finding.line, note="整条导入都不再需要"):
                return FixOutcome(finding.rule, finding.line, True, f"删除整条导入（{unused} 是唯一的名字）")
            return FixOutcome(finding.rule, finding.line, False, "", "该行已有其他编辑")
        new_text = f"{multi.group('indent')}from {multi.group('module')} import {', '.join(kept)}"
        if editor.replace(finding.line, new_text, note=f"摘掉未使用的 {unused}"):
            return FixOutcome(finding.rule, finding.line, True, f"从导入列表中摘掉 `{unused}`")
    return FixOutcome(finding.rule, finding.line, False, "", "导入写法较复杂，建议人工处理")


_MUTABLE_DEFAULT = re.compile(r"(?P<arg>\w+)\s*=\s*(?P<value>\[\s*\]|\{\s*\}|set\(\s*\)|list\(\s*\)|dict\(\s*\))")


def fix_mutable_default(editor: SourceEditor, finding: Any) -> FixOutcome:
    """可变默认参数 → ``None`` 哨兵 + 函数体内初始化。"""
    text = editor.line(finding.line)
    matches = list(_MUTABLE_DEFAULT.finditer(text))
    if not matches:
        return FixOutcome(finding.rule, finding.line, False, "", "该行未找到可变默认值")

    new_signature = _MUTABLE_DEFAULT.sub(lambda m: f"{m.group('arg')}=None", text)
    if not editor.replace(finding.line, new_signature, note="可变默认值改为 None 哨兵"):
        return FixOutcome(finding.rule, finding.line, False, "", "该行已有其他编辑")

    indent_match = _DEF_LINE.match(text)
    body_indent = (indent_match.group("indent") + "    ") if indent_match else "    "
    guards: list[str] = []
    for match in matches:
        arg, value = match.group("arg"), match.group("value").replace(" ", "")
        literal = {"[]": "[]", "{}": "{}", "set()": "set()", "list()": "[]", "dict()": "{}"}.get(value, "[]")
        guards.append(f"{body_indent}if {arg} is None:")
        guards.append(f"{body_indent}    {arg} = {literal}")
    # 签名可能跨多行，必须插到签名**结束**那一行之后；
    # 若函数已有 docstring，还要再往后跳过它，否则会把 docstring 挤成普通语句
    anchor = editor.body_anchor(editor.find_statement_end(finding.line))
    editor.insert_after(anchor, guards, note="函数体内初始化可变参数")

    args = "、".join(f"`{m.group('arg')}`" for m in matches)
    return FixOutcome(finding.rule, finding.line, True, f"可变默认参数 {args} 改为 None 哨兵并在函数体内初始化")


_SECRET_ASSIGN = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<quote>['\"])(?P<value>[^'\"]{6,})(?P=quote)(?P<tail>.*)$"
)


def fix_hardcoded_secret(editor: SourceEditor, finding: Any) -> FixOutcome:
    """硬编码凭据 → 从环境变量读取（并确保 ``import os``）。"""
    text = editor.line(finding.line)
    match = _SECRET_ASSIGN.match(text)
    if not match:
        return FixOutcome(finding.rule, finding.line, False, "", "该行不是简单的字符串赋值")
    name = match.group("name")
    env_key = re.sub(r"[^A-Za-z0-9]+", "_", name).upper()
    new_text = (f"{match.group('indent')}{name} = os.environ.get(\"{env_key}\", \"\")"
                f"{match.group('tail')}")
    if not editor.replace(finding.line, new_text, note=f"改为读取环境变量 {env_key}"):
        return FixOutcome(finding.rule, finding.line, False, "", "该行已有其他编辑")
    _ensure_import(editor, "os")
    return FixOutcome(finding.rule, finding.line, True,
                      f"硬编码凭据改为 `os.environ.get(\"{env_key}\", \"\")`（记得在部署环境注入）")


_DEF_DOCSTRING = re.compile(r"^(?P<indent>\s*)(?P<kw>async\s+def|def)\s+(?P<name>\w+)")


def fix_missing_docstring(editor: SourceEditor, finding: Any) -> FixOutcome:
    """公共函数缺文档 → 插入一行占位 docstring。"""
    text = editor.line(finding.line)
    match = _DEF_DOCSTRING.match(text)
    if not match:
        return FixOutcome(finding.rule, finding.line, False, "", "该行不是函数定义")
    signature_end = editor.find_statement_end(finding.line)
    body_indent = match.group("indent") + "    "
    editor.insert_after(signature_end, [f'{body_indent}"""{match.group("name")} 的说明（待补充）。"""'],
                        note="补占位文档字符串")
    return FixOutcome(finding.rule, finding.line, True, f"为 `{match.group('name')}` 补一行占位 docstring")


#: 规则名 → 修复函数
FIXERS: dict[str, Fixer] = {
    "none-comparison": fix_none_comparison,
    "bare-except": fix_bare_except,
    "except-pass": fix_except_pass,
    "unused-import": fix_unused_import,
    "mutable-default": fix_mutable_default,
    "hardcoded-secret": fix_hardcoded_secret,
    "missing-docstring": fix_missing_docstring,
}


# --------------------------------------------------------------------------- #
# 依赖注入类的小工具
# --------------------------------------------------------------------------- #
def _ensure_import(editor: SourceEditor, module: str) -> None:
    """确保顶部有 ``import <module>``；没有就在最后一条 import 之后插入。"""
    pattern = re.compile(rf"^\s*import\s+{re.escape(module)}\b|^\s*from\s+{re.escape(module)}\b")
    if any(pattern.match(line) for line in editor.lines):
        return
    last_import = 0
    for number, line in enumerate(editor.lines, start=1):
        if re.match(r"^\s*(import|from)\s+\w", line):
            last_import = number
    anchor = last_import or _after_module_docstring(editor)
    editor.insert_after(anchor, [f"import {module}"], note=f"补充 import {module}")


def _ensure_logger(editor: SourceEditor) -> None:
    """确保模块里有 ``logger``（补 ``import logging`` 与 logger 定义）。"""
    if any(re.match(r"^\s*logger\s*=", line) for line in editor.lines):
        return
    _ensure_import(editor, "logging")
    last_import = 0
    for number, line in enumerate(editor.lines, start=1):
        if re.match(r"^\s*(import|from)\s+\w", line):
            last_import = number
    anchor = last_import or _after_module_docstring(editor)
    editor.insert_after(anchor, ["", "logger = logging.getLogger(__name__)"], note="补充模块 logger")


def _after_module_docstring(editor: SourceEditor) -> int:
    """模块开头若是文档字符串，返回它的结束行；否则返回 0（插到文件最前）。"""
    if not editor.lines:
        return 0
    first = editor.lines[0].lstrip()
    if not first.startswith(('"""', "'''")):
        return 0
    quote = first[:3]
    if len(first) > 3 and first.rstrip().endswith(quote):
        return 1
    for number in range(2, len(editor.lines) + 1):
        if quote in editor.line(number):
            return number
    return 1


__all__ = [
    "Edit", "SourceEditor", "FixOutcome", "Fixer", "FIXERS", "MANUAL_RULES",
    "fix_none_comparison", "fix_bare_except", "fix_except_pass", "fix_unused_import",
    "fix_mutable_default", "fix_hardcoded_secret", "fix_missing_docstring",
]
