"""The compaction benchmark harness itself (oracle mode, no key, no network)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import compaction_bench as bench  # noqa: E402
from sessions import SESSIONS  # noqa: E402


class Sessions(unittest.TestCase):
    def test_labels_match_messages(self):
        for name, build in SESSIONS.items():
            b = build()
            ids = {m["tool_call_id"] for m in b.messages if m.get("role") == "tool"}
            self.assertEqual(ids, set(b.labels), name)
            self.assertEqual(set(b.labels.values()), {"needed", "stale"}, name)
            self.assertNotIn("stale", json.dumps(b.messages), name)  # labels never leak into the prompt


class Oracle(unittest.TestCase):
    def test_perfect_answers_give_zero_false_drops(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bench.main(["--oracle", "--json", str(Path(d) / "r.json")])
            rows = json.loads((Path(d) / "r.json").read_text())["rows"]
        self.assertEqual(rc, 0)
        self.assertIn("0/7 needed outputs wrongly stubbed", out.getvalue())
        for r in rows:
            self.assertEqual(r["false_drops"], [])
            self.assertEqual(r["kept_stale"], [])
            self.assertLess(r["tokens_after"], r["tokens_before"])


if __name__ == "__main__":
    unittest.main()
