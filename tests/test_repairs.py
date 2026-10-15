# -*- coding: utf-8 -*-
"""确定性修复规则测试。

重点覆盖两个真实踩过的坑（见下方 *Regression* 分节）：
多行函数签名被插坏、docstring 被守则挤成普通语句。
"""

from __future__ import annotations

import ast
import textwrap
import unittest

from codereview.report import Issue
from workflow.agents.repairs import FIXERS, MANUAL_RULES, SourceEditor


def issue(rule: str, line: int, title: str = "问题") -> Issue:
    return Issue(title=title, rule=rule, line=line, severity="major")


def apply(source: str, rule: str, line: int, title: str = "问题") -> str:
    """对源码执行一条修复规则并返回结果（同时断言结果语法合法）。"""
    editor = SourceEditor(textwrap.dedent(source).lstrip("\n"))
    outcome = FIXERS[rule](editor, issue(rule, line, title))
    assert outcome.applied, f"{rule} 未生效：{outcome.reason}"
    result = editor.render()
    ast.parse(result)  # 任何修复都不允许产出语法错误的代码
    return result


class SourceEditorTests(unittest.TestCase):
    def test_replace_delete_insert(self) -> None:
        editor = SourceEditor("a\nb\nc\n")
        editor.replace(2, "B")
        editor.insert_after(3, ["d"])
        self.assertEqual(editor.render(), "a\nB\nc\nd\n")

    def test_delete(self) -> None:
        editor = SourceEditor("a\nb\nc\n")
        editor.delete(2)
        self.assertEqual(editor.render(), "a\nc\n")

    def test_conflicting_edit_on_same_line_is_rejected(self) -> None:
        editor = SourceEditor("a\nb\n")
        self.assertTrue(editor.replace(1, "A"))
        self.assertFalse(editor.delete(1))
        self.assertTrue(editor.skipped)
        self.assertEqual(editor.render(), "A\nb\n")

    def test_out_of_range_line_is_skipped(self) -> None:
        editor = SourceEditor("a\n")
        self.assertFalse(editor.replace(99, "x"))
        self.assertTrue(any("超出文件范围" in reason for reason in editor.skipped))

    def test_find_line(self) -> None:
        editor = SourceEditor("a\n    pass\nb\n")
        self.assertEqual(editor.find_line(1, lambda s: s.strip() == "pass"), 2)
        self.assertIsNone(editor.find_line(1, lambda s: s.strip() == "nope", limit=2))

    def test_find_statement_end_single_line(self) -> None:
        editor = SourceEditor("def f(a):\n    return a\n")
        self.assertEqual(editor.find_statement_end(1), 1)

    def test_find_statement_end_multiline(self) -> None:
        editor = SourceEditor("def f(a,\n      b):\n    return a\n")
        self.assertEqual(editor.find_statement_end(1), 2)

    def test_body_anchor_skips_docstring(self) -> None:
        editor = SourceEditor('def f():\n    """说明。"""\n    return 1\n')
        self.assertEqual(editor.body_anchor(1), 2)

    def test_body_anchor_without_docstring(self) -> None:
        editor = SourceEditor("def f():\n    return 1\n")
        self.assertEqual(editor.body_anchor(1), 1)

    def test_body_anchor_multiline_docstring(self) -> None:
        editor = SourceEditor('def f():\n    """第一行\n    第二行。"""\n    return 1\n')
        self.assertEqual(editor.body_anchor(1), 3)

    def test_body_anchor_skips_blank_line_before_docstring(self) -> None:
        editor = SourceEditor('def f():\n\n    """说明。"""\n    return 1\n')
        self.assertEqual(editor.body_anchor(1), 3)


class IndividualFixerTests(unittest.TestCase):
    def test_none_comparison_eq(self) -> None:
        result = apply("def f(x):\n    if x == None:\n        return 1\n", "none-comparison", 2)
        self.assertIn("if x is None:", result)

    def test_none_comparison_ne(self) -> None:
        result = apply("def f(x):\n    if x != None:\n        return 1\n", "none-comparison", 2)
        self.assertIn("if x is not None:", result)

    def test_none_comparison_keeps_indentation(self) -> None:
        result = apply("def f(x):\n        if x  ==  None:\n            return 1\n", "none-comparison", 2)
        self.assertIn("        if x is None:", result)

    def test_bare_except_with_pass_becomes_logged(self) -> None:
        result = apply(
            "def f():\n    try:\n        pass\n    except:\n        pass\n",
            "bare-except", 4,
        )
        self.assertIn("except Exception:", result)
        self.assertIn("logger.warning", result)
        self.assertIn("import logging", result)
        self.assertIn("logger = logging.getLogger(__name__)", result)

    def test_bare_except_without_pass(self) -> None:
        result = apply("def f():\n    try:\n        pass\n    except:\n        return 1\n", "bare-except", 4)
        self.assertIn("except Exception:", result)
        self.assertNotIn("except:", result.replace("except Exception:", ""))

    def test_except_pass_records_log(self) -> None:
        result = apply("def f():\n    try:\n        pass\n    except ValueError:\n        pass\n", "except-pass", 4)
        self.assertIn("logger.warning", result)
        self.assertNotIn("        pass\n", result.split("except ValueError:")[1])

    def test_unused_import_single(self) -> None:
        result = apply("import os\nimport json\n", "unused-import", 1, title="未使用的导入 `os`")
        self.assertNotIn("import os", result)
        self.assertIn("import json", result)

    def test_unused_import_from_multi(self) -> None:
        result = apply("from os import path, getcwd\n", "unused-import", 1, title="未使用的导入 `getcwd`")
        self.assertIn("from os import path", result)
        self.assertNotIn("getcwd", result)

    def test_mutable_default_list(self) -> None:
        result = apply("def f(items=[]):\n    return items\n", "mutable-default", 1)
        self.assertIn("def f(items=None):", result)
        self.assertIn("if items is None:", result)
        self.assertIn("items = []", result)

    def test_mutable_default_dict(self) -> None:
        result = apply("def f(opts={}):\n    return opts\n", "mutable-default", 1)
        self.assertIn("def f(opts=None):", result)
        self.assertIn("opts = {}", result)

    def test_mutable_default_multiple_args(self) -> None:
        result = apply("def f(a=[], b={}):\n    return a, b\n", "mutable-default", 1)
        self.assertEqual(result.count("is None:"), 2)
        self.assertIn("def f(a=None, b=None):", result)

    def test_hardcoded_secret_moves_to_env(self) -> None:
        result = apply('TOKEN = "sk-real-looking-key"\n', "hardcoded-secret", 1)
        self.assertIn('os.environ.get("TOKEN", "")', result)
        self.assertIn("import os", result)
        self.assertNotIn("sk-real-looking-key", result)

    def test_missing_docstring_inserted(self) -> None:
        result = apply("def compute(a, b):\n    return a + b\n", "missing-docstring", 1)
        self.assertIn('"""compute 的说明（待补充）。"""', result)
        self.assertEqual(ast.get_docstring(ast.parse(result).body[0]), "compute 的说明（待补充）。")


class RegressionTests(unittest.TestCase):
    """两个真实 Bug 的回归测试——都是「修复把代码改坏」这一类。"""

    def test_guards_not_inserted_into_multiline_signature(self) -> None:
        """多行签名：守则必须插在签名结束之后，而不是参数列表中间。"""
        source = "def load(path, options={}, limit=None,\n         strict=False):\n    return path\n"
        result = apply(source, "mutable-default", 1)
        tree = ast.parse(result)  # 不抛异常即为通过
        function = tree.body[0]
        self.assertEqual(len(function.args.args), 4)
        # 守则应当位于函数体**开头**（在 return 之前），而不是在参数列表里
        self.assertLess(result.index("if options is None:"), result.index("return path"))
        self.assertGreater(result.index("if options is None:"), result.index("strict=False):"))

    def test_docstring_stays_first_after_mutable_default_fix(self) -> None:
        """守则不能插到 docstring 前面，否则 __doc__ 会变成 None 并触发新的审查问题。"""
        source = 'def load(options={}):\n    """读取配置。"""\n    return options\n'
        result = apply(source, "mutable-default", 1)
        function = ast.parse(result).body[0]
        self.assertEqual(ast.get_docstring(function), "读取配置。")
        self.assertIn("if options is None:", result)

    def test_none_comparison_does_not_double_space(self) -> None:
        result = apply("def f(x):\n    if x == None:\n        return 1\n", "none-comparison", 2)
        self.assertNotIn("  is None", result)
        self.assertIn("x is None", result)


class RegistryTests(unittest.TestCase):
    def test_every_fixer_is_registered_and_documented(self) -> None:
        for rule, fixer in FIXERS.items():
            self.assertTrue(callable(fixer), rule)
            self.assertTrue((fixer.__doc__ or "").strip(), f"{rule} 缺少说明")

    def test_unfixable_rules_have_explanations(self) -> None:
        for rule, reason in MANUAL_RULES.items():
            self.assertTrue(reason.strip(), rule)
            self.assertGreater(len(reason), 8, f"{rule} 的说明太短")

    def test_no_rule_is_both_auto_and_manual(self) -> None:
        overlap = set(FIXERS) & set(MANUAL_RULES)
        self.assertEqual(overlap, set(), f"同一规则不能既自动又手动：{overlap}")

    def test_missing_docstring_is_auto_fixable(self) -> None:
        self.assertIn("missing-docstring", FIXERS)
        self.assertNotIn("missing-docstring", MANUAL_RULES)


if __name__ == "__main__":
    unittest.main()
