import json
import os
import unittest
from unittest import mock

from helpers import EXAMPLES, noul

import jev_compact as c


def sample() -> list:
    return json.loads((EXAMPLES / "compact_sample.json").read_text(encoding="utf-8"))


def fake_jev(keep_call: float, keep_result: float):
    def ask(state, questions, model, timeout=60):
        ans = {}
        for key in questions:
            ans[key] = noul(keep_call if key.startswith("call_") else keep_result)
        return {"answers": ans}
    return ask


class Bridge(unittest.TestCase):
    def test_roundtrip_preserves_messages(self):
        msgs = sample()
        self.assertEqual(c.to_openai(c.from_openai(msgs)), msgs)


class Compact(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def compact(self, keep_call, keep_result, preserve=2):
        with mock.patch.object(c, "jev_ask", side_effect=fake_jev(keep_call, keep_result)):
            return c.compact_messages(
                sample(), model="m", keep_threshold=0.5, preserve_recent=preserve,
                max_state_tokens=25_000, max_request_tokens=30_000, truncate_head=300, goal="")

    def tool_ids(self, messages):
        calls = {tc["id"] for m in messages for tc in m.get("tool_calls") or []}
        results = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
        return calls, results

    def test_drop_removes_call_and_result_together(self):
        r = self.compact(0.1, 0.1)
        calls, results = self.tool_ids(r["messages"])
        self.assertEqual(calls, results)  # never an orphaned tool call or result
        self.assertEqual(calls, set())
        self.assertEqual(r["stats"]["callsDropped"], 2)
        self.assertGreater(r["stats"]["reduction"], 0)

    def test_keep_changes_nothing(self):
        r = self.compact(0.9, 0.9)
        self.assertEqual(r["messages"], sample())
        self.assertEqual(r["stats"]["kept"], 2)

    def test_user_and_assistant_text_stays_verbatim(self):
        r = self.compact(0.1, 0.1)
        texts = [m["content"] for m in r["messages"] if m["role"] in ("user", "assistant") and m.get("content")]
        expected = [m["content"] for m in sample() if m["role"] in ("user", "assistant") and m.get("content")]
        self.assertEqual(texts, expected)

    def test_recent_calls_are_pinned(self):
        r = self.compact(0.1, 0.1, preserve=10)
        self.assertEqual(r["stats"]["pinned"], 2)
        self.assertEqual(r["stats"]["requests"], 0)
        self.assertEqual(r["messages"], sample())

    def test_truncated_result_marks_itself(self):
        text = c.truncated_result("x" * 1000, False, 100)
        self.assertTrue(text.startswith("x" * 100))
        self.assertIn("jev-compact truncated 900 chars", text)
        self.assertEqual(c.truncated_result("short", False, 100), "short")


if __name__ == "__main__":
    unittest.main()
