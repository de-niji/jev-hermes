"""Hermes plugin wiring, tool handlers and install hygiene — no Hermes install, no network."""
import importlib.util
import json
import os
import re
import sys
import unittest
from unittest import mock

from helpers import ROOT, choice, noul

import hermes_tools as ht


def load_plugin_package():
    """Import the repo root the way Hermes does: as a package whose __init__ has register()."""
    spec = importlib.util.spec_from_file_location(
        "jev_plugin_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # relative imports need the package registered first
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    def __init__(self):
        self.tools, self.skills = {}, {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji="", **_):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "check_fn": check_fn}

    def register_skill(self, name, path, description="", **_):
        self.skills[name] = path


def manifest_list(key: str) -> list:
    """Tiny reader for a top-level YAML list in plugin.yaml (no PyYAML in CI)."""
    lines = (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{key}:")
    items = []
    for line in lines[start + 1:]:
        if not line.startswith("  - "):
            break
        items.append(line[4:].strip())
    return items


class Register(unittest.TestCase):
    def test_register_wires_tools_and_skill(self):
        ctx = FakeCtx()
        load_plugin_package().register(ctx)
        self.assertEqual(set(ctx.tools), set(manifest_list("provides_tools")))
        for name, t in ctx.tools.items():
            self.assertEqual(t["toolset"], "jev")
            self.assertEqual(t["schema"]["name"], name)
            self.assertTrue(callable(t["check_fn"]))
        self.assertTrue(ctx.skills["jev"].is_file())

    def test_check_fn_follows_key(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            self.assertTrue(ht.available())
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(ht.jd, "ENV_FILES", ()):
            self.assertFalse(ht.available())

    def test_schema_enums_match_presets(self):
        self.assertEqual(ht.DECIDE_SCHEMA["parameters"]["properties"]["preset"]["enum"], sorted(ht.jd.PRESETS))
        self.assertEqual(ht.TRIAGE_SCHEMA["parameters"]["properties"]["preset"]["enum"], sorted(ht.triage.PRESETS))


class Decide(unittest.TestCase):
    def call(self, params, fake=None):
        with mock.patch.object(ht.jd, "decide", side_effect=fake or (lambda s, q, m: {
                "answers": {"route": choice("calendar", 0.9), "needs_tools": noul(0.8)},
                "usage": {"cost": 2e-05}})) as m:
            out = json.loads(ht.handle_decide(params))
        return out, m

    def test_preset(self):
        out, m = self.call({"state": "What's on tomorrow?", "preset": "intent"})
        self.assertTrue(out["success"])
        self.assertEqual(out["brief"], "route=calendar(0.9) needs_tools=0.8 cost=2e-05")
        self.assertEqual(out["min_confidence"], 0.9)
        self.assertIs(m.call_args[0][1], ht.jd.PRESETS["intent"])

    def test_json_state_is_parsed(self):
        _, m = self.call({"state": '{"cmd": "ls"}', "preset": "approval"})
        self.assertEqual(m.call_args[0][0], {"cmd": "ls"})

    def test_custom_questions_pass_through(self):
        qs = {"q": {"type": "noul", "instructions": "?", "true": "y", "false": "n"}}
        _, m = self.call({"state": "x", "questions": qs})
        self.assertEqual(m.call_args[0][1], qs)

    def test_bad_input_fails_without_calling_jev(self):
        for params in ({}, {"state": "x"}, {"state": "x", "preset": "nope"}, {"state": "x", "questions": []}):
            out, m = self.call(params)
            self.assertFalse(out["success"], params)
            m.assert_not_called()

    def test_api_error_is_returned_not_raised(self):
        def boom(*_):
            raise ht.jd.JevError("HTTP 400: bad criteria")
        out, _ = self.call({"state": "x", "preset": "intent"}, boom)
        self.assertEqual(out, {"success": False, "error": "HTTP 400: bad criteria"})


class Triage(unittest.TestCase):
    MAILS = [
        {"id": "a", "from": "boss@example.com", "subject": "Need this today", "body": "SECRET BODY TEXT"},
        {"id": "b", "subject": "Sale", "labels": ["CATEGORY_PROMOTIONS"]},
    ]

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patchers = [mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
                    mock.patch.object(ht.jd, "decide", side_effect=lambda s, q, m: {
                        "answers": {"disposition": choice("urgent_reply", 0.4), "action_needed": noul(0.9)}})]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def call(self, params):
        real_run = ht.triage.run

        def run(args, mails=None):
            args.out = os.path.join(self.tmp.name, "out.json")
            return real_run(args, mails=mails)

        with mock.patch.object(ht.triage, "run", side_effect=run):
            return json.loads(ht.handle_triage(params))

    def test_inline_mails_return_summary_without_bodies(self):
        out = self.call({"mails": self.MAILS})
        self.assertTrue(out["success"])
        self.assertEqual(out["mail_count"], 2)
        self.assertEqual(out["counts"], {"urgent_reply": 1, "noise": 1})
        self.assertEqual([r["id"] for r in out["needs_attention"]], ["a"])
        self.assertEqual(out["escalate"], ["a"])  # confidence 0.4 < 0.5
        self.assertNotIn("SECRET BODY TEXT", json.dumps(out))  # bodies never reach the agent

    def test_max_is_clamped(self):
        out = self.call({"mails": self.MAILS * 100, "max": 10_000})
        self.assertEqual(out["mail_count"], ht.MAX_MAILS)

    def test_bad_input(self):
        self.assertFalse(self.call({"preset": "belege"})["success"])
        self.assertFalse(self.call({"mails": "a"})["success"])
        self.assertFalse(self.call({"max": "lots"})["success"])


class InstallHygiene(unittest.TestCase):
    def test_skill_references_files_the_skills_installer_copies(self):
        # When Hermes cannot list the repo tree it copies only support files SKILL.md references as
        # bare scripts/… or examples/… paths; ${HERMES_SKILL_DIR}/scripts/… alone does not count.
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("/opt/data/", skill)
        used = set(re.findall(r"\$\{HERMES_SKILL_DIR\}/((?:scripts|examples)/[\w./-]+)", skill))
        bare = set(re.findall(r"(?:^|[\s`(\"'])((?:scripts|examples)/[\w./-]+)", skill, re.M))
        self.assertTrue(used)
        self.assertLessEqual(used, bare)
        for path in bare:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_no_destructive_examples(self):
        # Hermes' install scanner rates `rm -rf` as HIGH, which blocks `hermes plugins install`.
        for path in ROOT.rglob("*"):
            if ".git" in path.parts or not path.is_file() or path.suffix not in (".md", ".py", ".json", ".yaml"):
                continue
            if path.name == "test_plugin.py":
                continue
            self.assertNotIn("rm -rf", path.read_text(encoding="utf-8"), path)


if __name__ == "__main__":
    unittest.main()
