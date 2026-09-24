import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import EXAMPLES, OFFLINE_URL, SCRIPTS, choice, noul

import jev_mail_triage as t


def answers(disp: str, conf: float, **signals: float) -> dict:
    return {"answers": {"disposition": choice(disp, conf), **{k: noul(v) for k, v in signals.items()}},
            "usage": {"cost": 2e-05}}


class Run(unittest.TestCase):
    """Runs main() in-process on examples/mails_sample.json with a fake Jev."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "out.json"
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, fake, *extra: str) -> dict:
        with mock.patch.object(t.jd, "decide", side_effect=fake) as m, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = t.main(["--input", str(EXAMPLES / "mails_sample.json"),
                         "--out", str(self.out), "--brief", *extra])
        self.assertEqual(rc, 0)
        self.calls = m.call_count
        return json.loads(self.out.read_text(encoding="utf-8"))

    def test_fastpath_skips_model(self):
        r = self.run_main(lambda s, q, m: answers("reference", 0.9, action_needed=0.1, has_deadline=0.1))
        self.assertEqual(self.calls, 2)  # m3 is CATEGORY_PROMOTIONS
        by_id = {row["id"]: row for row in r["rows"]}
        self.assertEqual(by_id["m3"]["source"], "label_fastpath")
        self.assertEqual(by_id["m3"]["disposition"], "noise")

    def test_body_from_input_is_used(self):
        seen = []
        self.run_main(lambda s, q, m: seen.append(s) or answers("reference", 0.9))
        self.assertIn("49.90 EUR", seen[0]["body"])
        self.assertNotIn("snippet", seen[0])

    def test_urgent_reply_needs_attention_even_with_low_signal(self):
        r = self.run_main(lambda s, q, m: answers("urgent_reply", 0.9, action_needed=0.2, has_deadline=0.1))
        self.assertEqual({row["id"] for row in r["needs_attention"]}, {"m1", "m2"})

    def test_low_confidence_escalates(self):
        r = self.run_main(lambda s, q, m: answers("reply", 0.3 if s["subject"].startswith("Can") else 0.9))
        self.assertEqual(r["escalate"], ["m2"])

    def test_failed_call_is_kept_and_escalated(self):
        def fake(state, qs, model):
            if state["subject"].startswith("Invoice"):
                raise t.jd.JevError("request failed: down")
            return answers("reply", 0.9)
        r = self.run_main(fake)
        by_id = {row["id"]: row for row in r["rows"]}
        self.assertEqual(by_id["m1"]["source"], "error")
        self.assertIn("m1", r["escalate"])
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(r["mail_count"], 3)

    def test_receipts_preset(self):
        r = self.run_main(lambda s, q, m: answers("receipt", 0.95, money_relevant=0.9, has_attachment=0.8),
                          "--preset", "receipts")
        self.assertEqual(r["preset"], "receipts")
        by_id = {row["id"]: row for row in r["rows"]}
        self.assertEqual(by_id["m3"]["disposition"], "info")
        self.assertIn("m1", {row["id"] for row in r["needs_attention"]})

    def test_missing_key_fails_before_any_call(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(t.jd, "ENV_FILES", ()):
            with self.assertRaises(t.jd.JevError):
                self.run_main(lambda s, q, m: self.fail("must not call Jev"))


class NeedsAttention(unittest.TestCase):
    def row(self, disp, **sig):
        return {"disposition": disp, "signals": {k: {"p": v, "yes": v >= 0.5} for k, v in sig.items()}}

    def test_inbox(self):
        for disp in ("urgent_reply", "reply", "action_no_reply"):
            self.assertTrue(t.needs_attention(self.row(disp, action_needed=0.1), "inbox"), disp)
        self.assertTrue(t.needs_attention(self.row("reference", action_needed=0.8), "inbox"))
        self.assertFalse(t.needs_attention(self.row("noise", action_needed=0.1), "inbox"))
        self.assertFalse(t.needs_attention(self.row(None), "inbox"))

    def test_receipts(self):
        for disp in ("receipt", "payment_issue", "contract"):
            self.assertTrue(t.needs_attention(self.row(disp), "receipts"), disp)
        self.assertTrue(t.needs_attention(self.row("unclear", money_relevant=0.9), "receipts"))
        self.assertFalse(t.needs_attention(self.row("info", money_relevant=0.9), "receipts"))


class Input(unittest.TestCase):
    def test_accepts_wrapped_object(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"mails": [{"id": "a"}, "junk"]}, f)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(t.load_input(f.name), [{"id": "a"}])

    def test_rejects_non_list(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"id": "a"}, f)
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(t.jd.JevError):
            t.load_input(f.name)


class Cli(unittest.TestCase):
    def test_stdin_with_api_down_keeps_all_mail(self):
        env = {**os.environ, "OPENROUTER_API_KEY": "x", "JEV_DECISIONS_URL": OFFLINE_URL}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.json"
            p = subprocess.run(
                [sys.executable, str(SCRIPTS / "jev_mail_triage.py"), "--input", "-",
                 "--out", str(out), "--brief"],
                input=(EXAMPLES / "mails_sample.json").read_text(encoding="utf-8"),
                capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            r = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(r["mail_count"], 3)
        self.assertEqual(sorted(r["escalate"]), ["m1", "m2"])
        self.assertIn("escalate=m1,m2", p.stdout)

    def test_old_preset_name_fails_loudly(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "jev_mail_triage.py"), "--preset", "belege"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("invalid choice", p.stderr)


if __name__ == "__main__":
    unittest.main()
