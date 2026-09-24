#!/usr/bin/env python3
"""Call TypeSafe Jev via OpenRouter Decisions API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
DECISIONS_URL = os.environ.get(
    "JEV_DECISIONS_URL", "https://openrouter.ai/api/alpha/decisions"
)


class JevError(RuntimeError):
    """API or config failure. CLIs exit 2 on this; batch callers catch it per item."""


# Question shapes follow OpenRouter Decisions / TypeSafe System One docs.
PRESETS: dict[str, dict[str, Any]] = {
    "intent": {
        "route": {
            "type": "choice",
            "instructions": (
                "Where should this user request go? Pick the best single bucket."
            ),
            "criteria": {
                "calendar": "Schedule, meetings, weekly plan, lectures, class timetable",
                "mail": "Email, inbox, invoices, send/read mail",
                "status": "Service health, uptime monitors, is X up?",
                "research": "Look something up, news, explain a topic",
                "complex": "Multi-step work, coding, ops changes, unclear or mixed",
            },
        },
        "needs_tools": {
            "type": "noul",
            "instructions": "Does answering require live tools (calendar, mail, shell, web)?",
            "true": "Needs live data or side effects",
            "false": "Can answer from chat text alone",
        },
    },
    "approval": {
        "escalate": {
            "type": "noul",
            "instructions": (
                "Should a human approve this shell/command before running? "
                "True for destructive, privilege escalation, secrets, network exfil, or ambiguous risk."
            ),
            "true": "Needs human approval",
            "false": "Safe read-only or clearly allowlisted",
        },
        "risk": {
            "type": "score",
            "instructions": "How risky is executing this?",
            "criteria": ["harmless read", "moderate change", "destructive / irreversible"],
        },
    },
    "mail_triage": {
        "kind": {
            "type": "choice",
            "instructions": "Classify this email for a personal-assistant morning brief.",
            "criteria": {
                "invoice": "Invoice, bill, payment due, payment request",
                "deadline": "Deadline, due date, registration or exam sign-up with a cutoff",
                "contract": "Contract, NDA, agreement to sign",
                "ignore": "Newsletter, promo, social, no action needed",
                "other": "Personal or work mail that is none of the above",
            },
        },
        "action_needed": {
            "type": "noul",
            "instructions": "Should this appear on today's focus list?",
            "true": "User should act or be reminded",
            "false": "No action this week",
        },
    },
}


# Hermes home: ~/.hermes by default, /opt/data in the official Docker image.
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

# Checked in order when the key is not in the environment: repo .env, then Hermes locations.
# (Hermes strips OPENROUTER_API_KEY from the env of scripts it runs, so the .env fallback matters.)
ENV_FILES: tuple[Path, ...] = tuple(dict.fromkeys((
    Path(__file__).resolve().parents[1] / ".env",
    HERMES_HOME / ".env",
    Path.home() / ".hermes" / ".env",
    Path("/opt/data/.env"),
)))


def _load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_TOKEN")
    if key:
        return key.strip()
    for p in ENV_FILES:
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"):
                return v.strip().strip('"').strip("'")
    raise JevError("OPENROUTER_API_KEY missing (env or .env)")


def _read_state(args: argparse.Namespace) -> Any:
    if args.state_file:
        raw = Path(args.state_file).read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if args.state is None:
        raise JevError("need --state or --state-file")
    text = args.state
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _read_questions(args: argparse.Namespace) -> dict[str, Any]:
    if args.questions_file:
        return json.loads(Path(args.questions_file).read_text(encoding="utf-8"))
    if args.preset:
        if args.preset not in PRESETS:
            raise JevError(f"unknown preset {args.preset}; choose {list(PRESETS)}")
        return PRESETS[args.preset]
    raise JevError("need --preset or --questions-file")


def decide(state: Any, questions: dict[str, Any], model: str) -> dict[str, Any]:
    body = {"model": model, "state": state, "questions": questions}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        DECISIONS_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {_load_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/de-niji/jev-hermes",
            "X-OpenRouter-Title": "jev-hermes",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise JevError(f"HTTP {e.code}: {err}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JevError(f"request failed: {e}") from e
    except json.JSONDecodeError as e:
        raise JevError(f"invalid JSON response: {e}") from e


def _brief(out: dict[str, Any]) -> str:
    answers = out.get("answers") or {}
    parts: list[str] = []
    for name, v in answers.items():
        if not isinstance(v, dict):
            continue
        if v.get("type") == "choice":
            parts.append(f"{name}={v.get('choice')}({v.get('confidence')})")
        elif v.get("type") == "noul":
            parts.append(f"{name}={v.get('noul')}")
        elif v.get("type") == "score":
            parts.append(f"{name}={v.get('score')}({v.get('confidence')})")
    cost = (out.get("usage") or {}).get("cost")
    if cost is not None:
        parts.append(f"cost={cost}")
    return " ".join(parts)


def _min_confidence(payload: dict[str, Any]) -> float | None:
    answers = payload.get("answers") or payload.get("result") or {}
    if not isinstance(answers, dict):
        return None
    vals: list[float] = []
    for v in answers.values():
        if isinstance(v, dict) and "confidence" in v:
            try:
                vals.append(float(v["confidence"]))
            except (TypeError, ValueError):
                pass
    return min(vals) if vals else None


def main() -> int:
    p = argparse.ArgumentParser(description="Jev decide via OpenRouter")
    p.add_argument("--state", help="State string or JSON")
    p.add_argument("--state-file", help="Path to state text/JSON")
    p.add_argument("--preset", choices=sorted(PRESETS), help="Built-in question set")
    p.add_argument("--questions-file", help="JSON map of questions")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="Exit 3 if any answer confidence is below this",
    )
    p.add_argument("--pretty", action="store_true")
    p.add_argument(
        "--brief",
        action="store_true",
        help="One flat line: route=calendar(1.0) needs_tools=0.91 cost=...",
    )
    args = p.parse_args()

    state = _read_state(args)
    questions = _read_questions(args)
    out = decide(state, questions, args.model)
    if args.brief:
        print(_brief(out))
    else:
        text = json.dumps(out, indent=2 if args.pretty else None, ensure_ascii=False)
        print(text)

    if args.min_confidence is not None:
        mc = _min_confidence(out)
        if mc is not None and mc < args.min_confidence:
            return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JevError as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    except BrokenPipeError:
        sys.exit(0)
