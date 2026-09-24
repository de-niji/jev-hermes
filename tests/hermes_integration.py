"""Check the plugin against a real Hermes Agent install (run by the `hermes` CI job).

Needs `hermes-agent` importable (pip install from the pinned commit). No API key or network:
it installs the plugin into a throwaway HERMES_HOME and checks what Hermes itself reports.

    python tests/hermes_integration.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="hermes-home-"))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    shutil.copytree(ROOT, home / "plugins" / "jev", ignore=shutil.ignore_patterns(".git", "__pycache__"))
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - jev\ncontext:\n  engine: jev\n", encoding="utf-8")

    # 1. `hermes plugins install` blocks community plugins unless the scan verdict is "safe".
    from tools.plugin_guard import scan_plugin
    scan = scan_plugin(ROOT, source="community")
    high = [f"{f.severity} {f.category} {f.file}" for f in scan.findings if f.severity != "medium"]
    check(scan.verdict == "safe", f"install scan verdict is safe (got {scan.verdict}; {high})")

    # 2. The plugin loads and registers its tools + skill.
    from hermes_cli import plugins
    plugins.discover_plugins()
    from tools.registry import registry
    for name in ("jev_decide", "jev_mail_triage"):
        entry = registry.get_entry(name)
        check(entry is not None and entry.toolset == "jev", f"{name} registered in toolset jev")
        check(bool(entry and entry.check_fn and entry.check_fn()), f"{name} available with a key")
    toolsets = [t[0] for t in plugins.get_plugin_toolsets()]
    check("jev" in toolsets, "jev toolset listed")
    skills = getattr(plugins.get_plugin_manager(), "_plugin_skills", {})
    check("jev:jev" in skills, "skill registered as jev:jev")

    # 3. Without a repo tree, Hermes copies only support files SKILL.md references as bare paths.
    from tools.skills_hub_models import _referenced_support_paths
    refs = _referenced_support_paths((ROOT / "SKILL.md").read_text(encoding="utf-8")) or set()
    for script in ("scripts/jev_decide.py", "scripts/jev_mail_triage.py", "scripts/jev_compact.py"):
        check(script in refs, f"SKILL.md references {script}")

    # 4. The risky-command gate is honoured by Hermes' real terminal tool dispatch (fake Jev).
    hooks = getattr(plugins.get_plugin_manager(), "_hooks", {}).get("pre_tool_call", [])
    check(any(type(h).__name__ == "Gate" for h in hooks), "pre_tool_call gate registered")
    import jev_decide
    asked: list[str] = []

    def fake_decide(state, questions, model, timeout=60):
        asked.append(state["cmd"])
        p = 0.95 if state["cmd"].startswith("kubectl") else 0.05
        return {"answers": {"escalate": {"type": "noul", "noul": p},
                            "risk": {"type": "score", "score": 2, "confidence": 0.9}}}

    jev_decide.decide = fake_decide
    from hermes_cli.plugins import _get_pre_tool_call_directive_details as directive
    risky = directive("terminal", {"command": "kubectl delete namespace prod"})
    check(risky.action == "approve" and (risky.rule_key or "").startswith("jev:"),
          f"risky command escalated to the human gate (got {risky.action})")
    builtin = directive("terminal", {"command": "git push --force origin main"})
    check(builtin.action is None and "git push --force origin main" not in asked,
          "command Hermes already flags is left to Hermes (no Jev call, no double prompt)")
    from model_tools import handle_function_call
    out = handle_function_call("terminal", {"command": "kubectl delete namespace prod"}, task_id="jev-it")
    check("BLOCKED" in out, "no human present: Hermes blocks the escalated command (fail closed)")
    out = handle_function_call("terminal", {"command": "echo jev-gate-ok"}, task_id="jev-it")
    check("jev-gate-ok" in out and "echo jev-gate-ok" in asked, "safe command runs after the Jev check")

    # 5. context.engine: jev selects the Jev engine; its prune stubs what Jev drops, keeps structure,
    #    and falls back to Hermes' built-in prune when Jev fails.
    import copy
    import json

    import jev_compact
    from agent.agent_init import _select_context_engine

    def new_engine():
        engine = _select_context_engine({"context": {"engine": "jev"}})
        if engine is not None:
            engine.update_model(model="anthropic/claude-sonnet-4", context_length=200_000, base_url="",
                                api_key="", provider="openrouter", api_mode="")
        return engine

    engine = new_engine()
    check(engine is not None and engine.name == "jev", "context.engine: jev selects the Jev engine")
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Fix the failing test"}]
    for i in range(12):
        cid = f"call_{i}"
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": f"cat f{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": f"# f{i}\n" + "x = 1\n" * 900})
        msgs.append({"role": "assistant", "content": f"read f{i}"})
    for i in range(25):
        msgs += [{"role": "user", "content": f"step {i}"}, {"role": "assistant", "content": f"done {i}"}]

    def fake_ask(state, questions, model, timeout=60):  # keep every third output
        return {"answers": {k: {"type": "noul", "noul": 0.9 if int(k.split("_t")[1]) % 3 == 0 else 0.1}
                            for k in questions}}

    def paired(ms):
        calls = {tc["id"] for m in ms for tc in (m.get("tool_calls") or [])}
        return calls == {m["tool_call_id"] for m in ms if m.get("role") == "tool"}

    jev_compact.jev_ask = fake_ask
    out, n = engine.prune_tool_results_only(copy.deepcopy(msgs), current_tokens=60_000)
    stubbed = [m["tool_call_id"] for m in out if m.get("role") == "tool"
               and str(m.get("content", "")).startswith("[jev-compact:")]
    check(len(stubbed) == 8 and "call_2" not in stubbed, f"early prune stubs what Jev drops (got {stubbed})")
    check(len(out) == len(msgs) and paired(out), "no message removed, every tool call keeps its result")
    check([m["content"] for m in out if m["role"] in ("user", "assistant")]
          == [m["content"] for m in msgs if m["role"] in ("user", "assistant")], "user/assistant text verbatim")

    def down(*a, **k):
        raise jev_decide.JevError("request failed: timed out")
    jev_compact.jev_ask = down
    engine = new_engine()
    try:
        out, n = engine.prune_tool_results_only(copy.deepcopy(msgs), current_tokens=60_000)
        ok = paired(out) and engine.jev_stats["errors"] == 1
    except Exception as e:  # noqa: BLE001
        ok = False
        print("   raised:", e)
    check(ok, "Jev down: falls back to Hermes' built-in prune without raising")

    shutil.rmtree(home, ignore_errors=True)
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
