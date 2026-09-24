"""Risky-command gate (pre_tool_call hook) with a fake Jev — no Hermes, no network."""
import unittest

from helpers import choice, noul

import hermes_gate as g


def settings(**overrides):
    def get_config(key, default=None):
        return overrides.get(key.split(".", 1)[1], default)
    return get_config


class FakeJev:
    def __init__(self, escalate=0.9, risk=2, error=None):
        self.escalate, self.risk, self.error = escalate, risk, error
        self.calls = []

    def __call__(self, state, questions, model, timeout=60):
        self.calls.append({"state": state, "questions": questions, "timeout": timeout})
        if self.error:
            raise g.jd.JevError(self.error)
        return {"answers": {"escalate": noul(self.escalate),
                            "risk": {"type": "score", "score": self.risk, "confidence": 0.9}}}


def gate(jev=None, builtin=lambda c: False, **cfg):
    jev = jev or FakeJev()
    return g.Gate(settings(**cfg), decide=jev, builtin=builtin), jev


CMD = {"command": "kubectl delete namespace prod"}


class Gate(unittest.TestCase):
    def test_risky_command_asks_the_user(self):
        hook, jev = gate()
        out = hook(tool_name="terminal", args=CMD, task_id="t", session_id="s")
        self.assertEqual(out["action"], "approve")
        self.assertIn("escalate=0.90", out["message"])
        self.assertTrue(out["rule_key"].startswith("jev:"))
        self.assertEqual(jev.calls[0]["state"], {"cmd": CMD["command"]})
        self.assertIs(jev.calls[0]["questions"], g.jd.PRESETS["approval"])
        self.assertEqual(jev.calls[0]["timeout"], 8.0)

    def test_safe_command_passes(self):
        hook, _ = gate(FakeJev(escalate=0.2))
        self.assertIsNone(hook(tool_name="terminal", args={"command": "ls -la"}))

    def test_threshold_is_configurable(self):
        hook, _ = gate(FakeJev(escalate=0.6))
        self.assertIsNone(hook(tool_name="terminal", args=CMD))
        hook, _ = gate(FakeJev(escalate=0.6), threshold=0.5)
        self.assertEqual(hook(tool_name="terminal", args=CMD)["action"], "approve")

    def test_block_mode(self):
        hook, _ = gate(mode="block")
        out = hook(tool_name="terminal", args=CMD)
        self.assertEqual(out["action"], "block")
        self.assertTrue(out["message"].startswith("BLOCKED:"))  # Hermes drops a block without message

    def test_disabled(self):
        hook, jev = gate(enabled=False)
        self.assertIsNone(hook(tool_name="terminal", args=CMD))
        self.assertEqual(jev.calls, [])

    def test_only_terminal_commands(self):
        hook, jev = gate()
        self.assertIsNone(hook(tool_name="write_file", args={"path": "x"}))
        self.assertIsNone(hook(tool_name="terminal", args={"command": "   "}))
        self.assertIsNone(hook(tool_name="terminal", args=None))
        self.assertEqual(jev.calls, [])

    def test_builtin_flagged_commands_are_left_to_hermes(self):
        # Hermes prompts for these itself; asking again would be a double prompt.
        hook, jev = gate(builtin=lambda c: True)
        self.assertIsNone(hook(tool_name="terminal", args=CMD))
        self.assertEqual(jev.calls, [])

    def test_jev_error_fails_open_by_default(self):
        hook, _ = gate(FakeJev(error="request failed: timed out"))
        self.assertIsNone(hook(tool_name="terminal", args=CMD))

    def test_jev_error_asks_user_when_fail_closed(self):
        hook, _ = gate(FakeJev(error="HTTP 500"), fail_closed=True)
        out = hook(tool_name="terminal", args=CMD)
        self.assertEqual(out["action"], "approve")
        self.assertIn("unavailable", out["message"])

    def test_decisions_are_cached_per_command(self):
        hook, jev = gate()
        hook(tool_name="terminal", args=CMD)
        hook(tool_name="terminal", args=CMD)
        hook(tool_name="terminal", args={"command": "kubectl delete namespace staging"})
        self.assertEqual(len(jev.calls), 2)

    def test_same_command_same_rule_key(self):
        hook, _ = gate()
        a = hook(tool_name="terminal", args=CMD)["rule_key"]
        b = hook(tool_name="terminal", args={"command": "kubectl delete namespace staging"})["rule_key"]
        self.assertEqual(a, hook(tool_name="terminal", args=CMD)["rule_key"])
        self.assertNotEqual(a, b)

    def test_malformed_answer_passes(self):
        class Weird(FakeJev):
            def __call__(self, *a, **k):
                return {"answers": {"escalate": {"type": "noul"}}}
        hook, _ = gate(Weird())
        self.assertIsNone(hook(tool_name="terminal", args=CMD))

    def test_builtin_flags_outside_hermes_is_false(self):
        self.assertFalse(g.builtin_flags("git push --force origin main"))

    def test_bad_setting_values_fall_back(self):
        hook, _ = gate(threshold=None, timeout=None)
        self.assertEqual(hook(tool_name="terminal", args=CMD)["action"], "approve")


if __name__ == "__main__":
    unittest.main()
