"""Hermes tool schemas + handlers for the jev plugin (registered by ``__init__.register``).

Kept free of Hermes imports so the offline tests can load it directly. Handlers never raise:
they return a JSON string with ``success`` and either the result or an ``error``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import jev_decide as jd  # noqa: E402
import jev_mail_triage as triage  # noqa: E402

TOOLSET = "jev"
MAX_MAILS = 100


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _fail(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def available() -> bool:
    """check_fn: tools are only offered when an OpenRouter key can be found."""
    try:
        jd._load_key()
    except jd.JevError:
        return False
    return True


# --- jev_decide -----------------------------------------------------------------------------

DECIDE_SCHEMA: dict[str, Any] = {
    "name": "jev_decide",
    "description": (
        "Ask TypeSafe Jev for a cheap typed decision (~$0.00002, sub-second) instead of reasoning "
        "it out yourself. Use it for narrow picks: route a request, judge whether a shell command "
        "is risky, classify a short text. Presets: 'intent' (route: calendar/mail/status/research/"
        "complex + needs_tools), 'approval' (escalate + risk for a shell command), 'mail_triage' "
        "(invoice/deadline/contract/ignore/other + action_needed). Or pass custom 'questions'. "
        "Each answer has a confidence; below 0.5 means unsure, so decide yourself or ask the user. "
        "Never use it to write text or plan multi-step work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "description": "What to decide about: the user message, the shell command, or a JSON string.",
            },
            "preset": {
                "type": "string",
                "enum": sorted(jd.PRESETS),
                "description": "Built-in question set. Omit when passing 'questions'.",
            },
            "questions": {
                "type": "object",
                "description": (
                    "Custom questions keyed by name. choice: {type:'choice', instructions, criteria:"
                    "{option: description}}; noul: {type:'noul', instructions, true, false}; score: "
                    "{type:'score', instructions, criteria:[level labels]}. Build the options yourself."
                ),
            },
        },
        "required": ["state"],
    },
}


def _parse_state(raw: Any) -> Any:
    if isinstance(raw, str) and raw.strip()[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def handle_decide(params: dict, **_: Any) -> str:
    state = params.get("state")
    if state in (None, ""):
        return _fail("'state' is required")
    questions = params.get("questions")
    preset = params.get("preset")
    if questions is None:
        if preset not in jd.PRESETS:
            return _fail(f"pass 'questions' or a preset from {sorted(jd.PRESETS)}")
        questions = jd.PRESETS[preset]
    elif not isinstance(questions, dict) or not questions:
        return _fail("'questions' must be a non-empty object")
    try:
        out = jd.decide(_parse_state(state), questions, jd.DEFAULT_MODEL)
    except jd.JevError as e:
        return _fail(str(e))
    return _ok(
        brief=jd._brief(out),
        answers=out.get("answers") or {},
        min_confidence=jd._min_confidence(out),
        cost=(out.get("usage") or {}).get("cost"),
    )


# --- jev_mail_triage ------------------------------------------------------------------------

TRIAGE_SCHEMA: dict[str, Any] = {
    "name": "jev_mail_triage",
    "description": (
        "Triage mail with Jev without reading bodies into this conversation. Lists Gmail (Google "
        "Workspace skill) for 'query', or takes 'mails' you already have, and returns only counts, "
        "the mails that need the user (needs_attention) and the ids Jev was unsure about "
        "(escalate). Preset 'inbox': urgent_reply/reply/action_no_reply/waiting/reference/noise. "
        "Preset 'receipts': receipt/payment_issue/contract/info/unclear (gate before filing "
        "invoices). Only open escalated mails yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "preset": {"type": "string", "enum": sorted(triage.PRESETS), "description": "Default 'inbox'."},
            "query": {
                "type": "string",
                "description": "Gmail search query, e.g. 'newer_than:2d -in:chats'. Ignored with 'mails'.",
            },
            "max": {"type": "integer", "minimum": 1, "maximum": MAX_MAILS, "description": "Default 25."},
            "mails": {
                "type": "array",
                "description": "Mails to triage instead of Gmail: [{id, from, subject, date, labels, body|snippet}].",
                "items": {"type": "object"},
            },
        },
    },
}


def _attention_row(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "from", "subject", "date", "disposition", "confidence", "escalate")}


def handle_triage(params: dict, **_: Any) -> str:
    preset = params.get("preset") or "inbox"
    if preset not in triage.PRESETS:
        return _fail(f"unknown preset {preset!r}; choose {sorted(triage.PRESETS)}")
    try:
        max_n = max(1, min(int(params.get("max") or 25), MAX_MAILS))
    except (TypeError, ValueError):
        return _fail("'max' must be an integer")
    mails = params.get("mails")
    if mails is not None and not isinstance(mails, list):
        return _fail("'mails' must be an array")
    argv = ["--preset", preset, "--max", str(max_n)]
    if params.get("query"):
        argv += ["--query", str(params["query"])]
    args = triage.build_parser().parse_args(argv)
    try:
        result = triage.run(args, mails=mails)
    except jd.JevError as e:
        return _fail(str(e))
    except RuntimeError as e:  # Gmail listing failed
        return _fail(f"mail listing failed: {e}")
    return _ok(
        preset=result["preset"],
        mail_count=result["mail_count"],
        counts=result["counts"],
        needs_attention=[_attention_row(r) for r in result["needs_attention"]],
        escalate=result["escalate"],
        errors=len(result["errors"]),
        cost_usd=result["cost_usd"],
        out=result["out"],
    )


TOOLS = (
    ("jev_decide", DECIDE_SCHEMA, handle_decide, "⚖️"),
    ("jev_mail_triage", TRIAGE_SCHEMA, handle_triage, "📬"),
)
