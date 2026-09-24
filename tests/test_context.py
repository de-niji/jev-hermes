"""Jev context engine logic (candidates, stubbing, settings) — no Hermes, no network."""
import json
import unittest

import hermes_context as hc


def session(n_reads=6, size=1200, tail=4):
    """system, user, n tool reads (call + result + note), then a chatty tail."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Fix the failing test"}]
    for i in range(n_reads):
        cid = f"c{i}"
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "terminal", "arguments": json.dumps({"command": f"cat f{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": f"# f{i}\n" + "x" * size, "_db_persisted": True})
        msgs.append({"role": "assistant", "content": f"read f{i}"})
    for i in range(tail):
        msgs += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    return msgs


def scorer(keep: dict):
    calls = []

    def score(messages, ids):
        calls.append(set(ids))
        return {cid: {"keepCall": 0.9, "keepResult": keep.get(cid, 0.9)} for cid in ids}
    score.calls = calls
    return score


class Candidates(unittest.TestCase):
    def test_only_old_long_outputs(self):
        msgs = session()
        boundary = len(msgs)
        self.assertEqual(set(hc.candidates(msgs, boundary, 800)), {f"c{i}" for i in range(6)})
        self.assertEqual(hc.candidates(msgs, boundary, 5000), {})

    def test_boundary_protects_the_tail(self):
        msgs = session()
        # boundary right after c2's result: c3.. are in the protected tail
        boundary = next(i for i, m in enumerate(msgs) if m.get("tool_call_id") == "c2") + 1
        self.assertEqual(set(hc.candidates(msgs, boundary, 800)), {"c0", "c1", "c2"})
        # a call before the boundary whose result is after it is not a candidate
        self.assertNotIn("c2", hc.candidates(msgs, boundary - 1, 800))

    def test_skill_views_and_existing_stubs_are_skipped(self):
        msgs = session(n_reads=2)
        msgs[2]["tool_calls"][0]["function"]["name"] = "skill_view"
        msgs[6]["content"] = hc.stub_text("terminal", "y" * 2000) + "z" * 900
        self.assertEqual(hc.candidates(msgs, len(msgs), 800), {})

    def test_non_string_content_is_skipped(self):
        msgs = session(n_reads=1)
        msgs[3]["content"] = [{"type": "text", "text": "x" * 2000}]
        self.assertEqual(hc.candidates(msgs, len(msgs), 800), {})


class Stub(unittest.TestCase):
    def test_stubs_only_what_jev_drops(self):
        msgs = session()
        score = scorer({"c0": 0.1, "c3": 0.2, "c4": 0.6})
        out, n = hc.jev_stub(msgs, len(msgs), score, keep_threshold=0.5, min_chars=800)
        self.assertEqual(n, 2)
        stubbed = {m["tool_call_id"] for m in out if m.get("role") == "tool" and m["content"].startswith(hc.STUB_PREFIX)}
        self.assertEqual(stubbed, {"c0", "c3"})
        self.assertIn("1205 chars", next(m for m in out if m.get("tool_call_id") == "c0")["content"])

    def test_structure_and_text_are_preserved(self):
        msgs = session()
        out, _ = hc.jev_stub(msgs, len(msgs), scorer({f"c{i}": 0.0 for i in range(6)}),
                             keep_threshold=0.5, min_chars=800)
        self.assertEqual(len(out), len(msgs))
        for before, after in zip(msgs, out):
            self.assertEqual(before["role"], after["role"])
            if before["role"] != "tool":
                self.assertIs(before, after)  # untouched messages are the same objects
            else:
                self.assertEqual(before["tool_call_id"], after["tool_call_id"])
                self.assertTrue(after["_db_persisted"])  # shallow copy keeps Hermes' markers
        self.assertNotEqual(msgs[3]["content"], out[3]["content"])
        self.assertTrue(msgs[3]["content"].startswith("# f0"))  # input not mutated

    def test_nothing_dropped_returns_input_object(self):
        msgs = session()
        out, n = hc.jev_stub(msgs, len(msgs), scorer({}), keep_threshold=0.5, min_chars=800)
        self.assertIs(out, msgs)
        self.assertEqual(n, 0)

    def test_no_candidates_means_no_jev_call(self):
        msgs = session(size=100)
        score = scorer({})
        out, n = hc.jev_stub(msgs, len(msgs), score, keep_threshold=0.5, min_chars=800)
        self.assertEqual((out, n), (msgs, 0))
        self.assertEqual(score.calls, [])

    def test_scores_for_unknown_ids_are_ignored(self):
        msgs = session(n_reads=1)

        def score(messages, ids):
            return {"c0": {"keepResult": 0.9}, "not-a-candidate": {"keepResult": 0.0}}
        self.assertEqual(hc.jev_stub(msgs, len(msgs), score, keep_threshold=0.5, min_chars=800)[1], 0)

    def test_jev_error_propagates_to_the_engine(self):
        msgs = session()

        def score(messages, ids):
            raise hc.jd.JevError("down")
        with self.assertRaises(hc.jd.JevError):
            hc.jev_stub(msgs, len(msgs), score, keep_threshold=0.5, min_chars=800)


class Settings(unittest.TestCase):
    def test_defaults_and_overrides(self):
        self.assertEqual(hc.settings(lambda k, d=None: d), hc.DEFAULTS)
        got = hc.settings(lambda k, d=None: {"compact.keep_threshold": 0.3}.get(k, d))
        self.assertEqual(got["keep_threshold"], 0.3)

    def test_compressor_kwargs_follow_compression_block(self):
        kw = hc._compressor_kwargs({"compression": {"threshold": 0.6, "protect_last_n": 12,
                                                    "tail_mode": "legacy"}}, 48000)
        self.assertEqual(kw["threshold_percent"], 0.6)
        self.assertEqual(kw["protect_last_n"], 12)
        self.assertEqual(kw["tail_mode"], "legacy")
        self.assertEqual(kw["proactive_prune_tokens"], 48000)  # early prune on by default

    def test_explicit_prune_trigger_wins(self):
        kw = hc._compressor_kwargs({"compression": {"proactive_prune_tokens": 20000}}, 48000)
        self.assertEqual(kw["proactive_prune_tokens"], 20000)

    def test_garbage_values_are_ignored(self):
        kw = hc._compressor_kwargs({"compression": {"threshold": "lots", "proactive_prune_tokens": "x"}}, 48000)
        self.assertNotIn("threshold_percent", kw)
        self.assertEqual(kw["proactive_prune_tokens"], 48000)


if __name__ == "__main__":
    unittest.main()
