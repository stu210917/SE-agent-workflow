# -*- coding: utf-8 -*-
"""共享状态（黑板）与消息总线测试。"""

from __future__ import annotations

import json
import unittest

from workflow.errors import StateKeyError, StateTypeError
from workflow.message import BROADCAST, MessageBus
from workflow.state import WorkflowState


class StateBasicsTests(unittest.TestCase):
    def test_get_and_require(self) -> None:
        state = WorkflowState({"spec": "写个工具"})
        self.assertEqual(state.get("spec"), "写个工具")
        self.assertEqual(state.get("missing", "默认"), "默认")
        self.assertEqual(state.require("spec"), "写个工具")

    def test_require_missing_key_lists_available_keys(self) -> None:
        state = WorkflowState({"code": "x"})
        with self.assertRaises(StateKeyError) as ctx:
            state.require("findings")
        # 报错信息要能直接指导排错
        self.assertIn("findings", str(ctx.exception))
        self.assertIn("code", str(ctx.exception))

    def test_require_checks_type(self) -> None:
        state = WorkflowState({"count": 3})
        self.assertEqual(state.require("count", int), 3)
        with self.assertRaises(StateTypeError):
            state.require("count", str)

    def test_has_and_keys(self) -> None:
        state = WorkflowState({"b": 1, "a": 2})
        self.assertIn("a", state)
        self.assertTrue(state.has("b"))
        self.assertEqual(state.keys(), ["a", "b"])
        self.assertEqual(len(state), 2)


class StateVersioningTests(unittest.TestCase):
    """循环流程必须能拿到「同一份产物的历史版本」。"""

    def test_set_creates_versions(self) -> None:
        state = WorkflowState()
        state.set("code", "v1", by="coder")
        state.set("code", "v2", by="fixer")
        state.set("code", "v3", by="fixer")

        self.assertEqual(state.get("code"), "v3")
        self.assertEqual(state.versions("code"), ["v1", "v2", "v3"])
        self.assertEqual(state.previous("code"), "v2")
        self.assertEqual(state.previous("code", back=2), "v1")

    def test_previous_beyond_history_raises(self) -> None:
        state = WorkflowState()
        state.set("code", "v1", by="coder")
        with self.assertRaises(StateKeyError):
            state.previous("code", back=5)

    def test_change_log_records_who_wrote_what(self) -> None:
        state = WorkflowState()
        state.step = 2
        state.set("code", "print(1)", by="coder", note="初稿")
        state.set("review", {"score": 80}, by="reviewer", note="第 1 轮")

        changes = state.changes()
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0].by, "coder")
        self.assertEqual(changes[0].step, 2)
        self.assertEqual(changes[0].version, 1)
        self.assertEqual([change.by for change in state.changes_by("reviewer")], ["reviewer"])
        self.assertEqual([change.key for change in state.changes_of_key("code")], ["code"])

    def test_changes_by_and_of_key_are_different_filters(self) -> None:
        """这两个概念很容易混：一个按写入者，一个按状态键。"""
        state = WorkflowState()
        state.set("code", "v1", by="coder")
        state.set("code", "v2", by="fixer")
        state.set("review", {}, by="reviewer")

        self.assertEqual(len(state.changes_of_key("code")), 2)
        self.assertEqual(len(state.changes_by("fixer")), 1)
        self.assertEqual(state.changes_by("code"), [])  # 没有叫 code 的写入者

    def test_increment_helper(self) -> None:
        state = WorkflowState()
        self.assertEqual(state.increment("round", by="fixer"), 1)
        self.assertEqual(state.increment("round", by="fixer"), 2)
        self.assertEqual(state.get("round"), 2)

    def test_snapshot_truncates_long_text(self) -> None:
        state = WorkflowState({"code": "x" * 5000, "count": 3})
        snapshot = state.snapshot(max_text=50)
        self.assertIn("5000 字符", snapshot["code"])
        self.assertEqual(snapshot["count"], 3)
        json.dumps(snapshot, ensure_ascii=False)  # 必须可序列化

    def test_update_sets_multiple_keys(self) -> None:
        state = WorkflowState()
        changes = state.update({"a": 1, "b": 2}, by="node")
        self.assertEqual(len(changes), 2)
        self.assertEqual(state.get("b"), 2)


class MessageBusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = MessageBus()

    def test_directed_message_and_inbox(self) -> None:
        self.bus.send("reviewer", "fixer", "review_done", {"total": 5})
        inbox = self.bus.inbox("fixer")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].topic, "review_done")
        self.assertEqual(inbox[0].payload["total"], 5)
        self.assertEqual(self.bus.inbox("coder"), [])

    def test_broadcast_reaches_everyone(self) -> None:
        self.bus.broadcast("engine", "tick", {"round": 1})
        self.assertTrue(self.bus.inbox("coder")[0].is_broadcast)
        self.assertTrue(self.bus.inbox("fixer")[0].is_broadcast)
        self.assertEqual(self.bus.history()[0].recipient, BROADCAST)

    def test_seq_is_monotonic(self) -> None:
        first = self.bus.send("a", "b", "t1")
        second = self.bus.send("b", "a", "t2")
        self.assertEqual((first.seq, second.seq), (1, 2))

    def test_between_filters_by_pair(self) -> None:
        self.bus.send("reviewer", "fixer", "review_done")
        self.bus.send("fixer", "reviewer", "fix_applied")
        self.assertEqual(len(self.bus.between("reviewer", "fixer")), 1)

    def test_inbox_since_returns_only_new(self) -> None:
        self.bus.send("a", "b", "one")
        self.bus.send("a", "b", "two")
        self.assertEqual([m.topic for m in self.bus.inbox("b", since=1)], ["two"])

    def test_message_is_immutable_and_serializable(self) -> None:
        message = self.bus.send("a", "b", "topic", {"summary": "很长的说明" * 40})
        with self.assertRaises(Exception):
            message.topic = "changed"  # frozen dataclass
        json.dumps(message.to_dict(), ensure_ascii=False)
        self.assertTrue(message.summary().endswith("…"))

    def test_render_is_human_readable(self) -> None:
        self.bus.send("reviewer", "fixer", "review_done", {"summary": "3 条问题"})
        text = self.bus.render()
        self.assertIn("reviewer → fixer", text)
        self.assertIn("review_done", text)

    def test_empty_bus_render(self) -> None:
        self.assertIn("没有产生任何消息", self.bus.render())


if __name__ == "__main__":
    unittest.main()
