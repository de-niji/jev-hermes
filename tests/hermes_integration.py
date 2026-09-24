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
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - jev\n", encoding="utf-8")

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

    shutil.rmtree(home, ignore_errors=True)
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
