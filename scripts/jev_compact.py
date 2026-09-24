#!/usr/bin/env python3
"""Compact conversation tool history with TypeSafe Jev (no LLM summary).

Inspired by https://github.com/tamaratran/fast-jev-compaction — adapted for
OpenRouter Decisions API and Hermes-style OpenAI chat messages.

User/assistant text stays verbatim. Only tool calls/results may be dropped or
truncated based on Jev noul scores.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Reuse auth + HTTP from sibling script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_decide import JevError, _load_key, decide as jev_ask  # noqa: E402

DEFAULT_MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")

STATE_CONTEXT = (
    "An assistant conversation is being compacted to free context. `history` is "
    "the whole conversation so far, oldest first; tool outputs are replaced by a "
    "short `result` note and long texts may be abridged. Each question asks whether "
    "one tool call, or the full output of that call, still needs to stay in the "
    "history verbatim. Whatever is not kept is deleted permanently, but the "
    "assistant can always re-run a tool."
)

INPUT_CHARS = (1000, 200, 60)
TEXT_HEAD = 400
TEXT_TAIL = 150
TOKEN_PIECES = __import__("re").compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]+")
REQUEST_OVERHEAD = 20


def estimate_tokens(text: str) -> int:
    tokens = 0.0
    for m in TOKEN_PIECES.finditer(text):
        piece = m.group(0)
        first = ord(piece[0])
        if 48 <= first <= 57:
            tokens += len(piece) / 2
        elif (65 <= first <= 90) or (97 <= first <= 122):
            tokens += 1 + (len(piece) - 1) // 6
        else:
            tokens += 0.9
    return int(tokens + 0.999)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def abridge(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail + 40:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[… {omitted} chars omitted …]\n{text[-tail:]}"


# --- OpenAI / Hermes message bridge -------------------------------------------------


def from_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI chat messages to internal {role,text,toolUses,toolResults}."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role") or "user"
        content = m.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif isinstance(p, str):
                    parts.append(p)
            text = "\n".join(parts)
        else:
            text = str(content)

        tool_uses: list[dict[str, Any]] = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    inp = json.loads(args)
                except json.JSONDecodeError:
                    inp = {"_raw": args}
            else:
                inp = args if isinstance(args, dict) else {"_raw": args}
            tool_uses.append(
                {
                    "tool_use_id": tc.get("id") or f"call_{len(tool_uses)}",
                    "tool": fn.get("name") or "unknown",
                    "input": inp if isinstance(inp, dict) else {"value": inp},
                }
            )

        tool_results: list[dict[str, Any]] = []
        if role == "tool":
            tool_results.append(
                {
                    "tool_use_id": m.get("tool_call_id") or "",
                    "text": text,
                    "isError": bool(m.get("is_error")),
                }
            )
            text = ""
            role = "user"  # tool results attach as user-side in Claude-style; keep separate

        entry: dict[str, Any] = {"role": role, "text": text, "toolUses": tool_uses}
        if tool_results:
            entry["toolResults"] = tool_results
        # Skip empty
        if not text.strip() and not tool_uses and not tool_results:
            continue
        out.append(entry)
    return out


def to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal messages back to OpenAI chat format."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role") or "user"
        text = m.get("text") or ""
        tool_uses = m.get("toolUses") or []
        tool_results = m.get("toolResults") or []

        if tool_results and not tool_uses and not text.strip():
            for r in tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": r.get("tool_use_id"),
                        "content": r.get("text") or "",
                    }
                )
            continue

        msg: dict[str, Any] = {"role": role, "content": text}
        if tool_uses:
            msg["tool_calls"] = [
                {
                    "id": t["tool_use_id"],
                    "type": "function",
                    "function": {
                        "name": t.get("tool") or "unknown",
                        "arguments": json.dumps(t.get("input") or {}, ensure_ascii=False),
                    },
                }
                for t in tool_uses
            ]
            if not text:
                msg["content"] = None
        out.append(msg)

        for r in tool_results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": r.get("tool_use_id"),
                    "content": r.get("text") or "",
                }
            )
    return out


# --- Collect / fit / decide ---------------------------------------------------------


def is_pinned(index: int, total: int, preserve: int) -> bool:
    return index == 0 or index >= total - preserve


def collect_tool_calls(messages: list[dict[str, Any]], preserve: int) -> list[dict[str, Any]]:
    results: dict[str, tuple[int, dict[str, Any]]] = {}
    for i, message in enumerate(messages):
        for result in message.get("toolResults") or []:
            results[result["tool_use_id"]] = (i, result)

    calls: list[dict[str, Any]] = []
    for call_index, message in enumerate(messages):
        for tool in message.get("toolUses") or []:
            found = results.get(tool["tool_use_id"])
            if not found:
                continue
            result_index, result = found
            calls.append(
                {
                    "id": f"t{len(calls) + 1}",
                    "tool_use_id": tool["tool_use_id"],
                    "tool": tool.get("tool") or "unknown",
                    "input": tool.get("input") or {},
                    "callIndex": call_index,
                    "resultIndex": result_index,
                    "resultChars": len(result.get("text") or ""),
                    "isError": bool(result.get("isError")),
                    "pinned": is_pinned(call_index, len(messages), preserve)
                    or is_pinned(result_index, len(messages), preserve),
                }
            )
    return calls


def input_text(inp: dict[str, Any], limit: int) -> str:
    try:
        raw = json.dumps(inp, ensure_ascii=False)
    except TypeError:
        raw = "[unserializable input]"
    return truncate(raw, limit)


def result_note(call: dict[str, Any]) -> str:
    kind = "error" if call["isError"] else "ok"
    return f"{kind}, {call['resultChars']} chars (omitted)"


def compact_call_line(call: dict[str, Any]) -> str:
    parts = []
    for key, value in (call.get("input") or {}).items():
        text = value if isinstance(value, str) else input_text({key: value}, 200)
        parts.append(f"{key}={str(text).replace(chr(10), ' ')}")
    joined = " ".join(parts)
    kind = "error" if call["isError"] else "ok"
    return f"{call['id']} {call['tool']} {truncate(joined, INPUT_CHARS[2])} → {kind} {call['resultChars']}ch"


def goal_from_messages(messages: list[dict[str, Any]]) -> str:
    users = [
        m
        for m in messages
        if m.get("role") == "user"
        and (m.get("text") or "").strip()
        and not (m.get("toolResults") or [])
    ]
    return "\n".join(truncate(m["text"], 500) for m in users[-3:])


def history_entries(
    messages: list[dict[str, Any]], calls: list[dict[str, Any]], input_chars: int
) -> list[dict[str, Any]]:
    by_msg: dict[int, list[dict[str, Any]]] = {}
    for call in calls:
        by_msg.setdefault(call["callIndex"], []).append(call)
    entries: list[dict[str, Any]] = []
    for i, message in enumerate(messages):
        tool_calls = [
            {
                "id": c["id"],
                "tool": c["tool"],
                "input": input_text(c["input"], input_chars),
                "result": result_note(c),
            }
            for c in by_msg.get(i, [])
        ]
        if not (message.get("text") or "").strip() and not tool_calls:
            continue
        entry: dict[str, Any] = {"i": i, "role": message.get("role"), "text": message.get("text") or ""}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        entries.append(entry)
    return entries


def fit_state(
    messages: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    *,
    max_state_tokens: int,
    preserve: int,
    goal: str,
) -> tuple[dict[str, Any], int, str]:
    goal = goal or goal_from_messages(messages)

    def state_of(history: list[dict[str, Any]]) -> dict[str, Any]:
        return {"context": STATE_CONTEXT, "goal": goal, "history": history}

    def entry_tokens(entry: dict[str, Any]) -> int:
        return estimate_tokens(json.dumps(entry, ensure_ascii=False)) + 1

    base = estimate_tokens(json.dumps(state_of([]), ensure_ascii=False))
    history: list[dict[str, Any]] = []
    per_entry: list[int] = []
    tokens = 0

    def rebuild(limit: int) -> None:
        nonlocal history, per_entry, tokens
        history = history_entries(messages, calls, limit)
        per_entry = [entry_tokens(e) for e in history]
        tokens = base + sum(per_entry)

    def fits() -> bool:
        return tokens <= max_state_tokens

    def shrink(index: int, change) -> None:
        nonlocal tokens
        entry = history[index]
        change(entry)
        now = entry_tokens(entry)
        tokens += now - per_entry[index]
        per_entry[index] = now

    rebuild(INPUT_CHARS[0])
    if fits():
        return state_of(history), tokens, "full"

    for limit in INPUT_CHARS[1:]:
        rebuild(limit)
        if fits():
            return state_of(history), tokens, f"inputs<={limit}"

    def pinned(entry: dict[str, Any]) -> bool:
        return is_pinned(entry["i"], len(messages), preserve)

    indices = list(range(len(history)))
    order = [i for i in indices if not pinned(history[i])] + [
        i for i in indices if pinned(history[i])
    ]

    for index in order:
        entry = history[index]
        if len(entry["text"]) <= TEXT_HEAD + TEXT_TAIL + 40:
            continue
        shrink(index, lambda e: e.update(text=abridge(e["text"], TEXT_HEAD, TEXT_TAIL)))
        if fits():
            return state_of(history), tokens, "texts abridged"

    for index in order:
        entry = history[index]
        if pinned(entry) or not entry["text"]:
            continue
        original = len(messages[entry["i"]].get("text") or entry["text"])
        shrink(index, lambda e, o=original: e.update(text=f"[… {o} chars omitted …]"))
        if fits():
            return state_of(history), tokens, "old messages collapsed"

    by_msg: dict[int, list[dict[str, Any]]] = {}
    for call in calls:
        by_msg.setdefault(call["callIndex"], []).append(call)

    for index in order:
        entry = history[index]
        own = by_msg.get(entry["i"])
        if pinned(entry) or not own:
            continue
        shrink(index, lambda e, o=own: e.update(tool_calls=[compact_call_line(c) for c in o]))
        if fits():
            return state_of(history), tokens, "old calls compacted"

    left: set[int] = set()
    for index in order:
        entry = history[index]
        if pinned(entry) or entry.get("tool_calls"):
            continue
        left.add(index)
        tokens -= per_entry[index]
        if fits():
            hist = [e for i, e in enumerate(history) if i not in left]
            return state_of(hist), tokens, "old messages left out"

    raise SystemExit(
        f"history too large for Jev (~{tokens} tokens after truncation, limit {max_state_tokens})"
    )


def questions_for(call: dict[str, Any]) -> dict[str, Any]:
    return {
        f"call_{call['id']}": {
            "type": "noul",
            "instructions": (
                f"Tool call {call['id']} ({call['tool']}) should stay in the history: "
                "knowing this call was made, with its input, still matters for what the assistant does next"
            ),
            "true": "Keep the call record",
            "false": "Safe to remove the call and its result",
        },
        f"result_{call['id']}": {
            "type": "noul",
            "instructions": (
                f"The full output of tool call {call['id']} ({call['tool']}, "
                f"{call['resultChars']} chars) should stay verbatim: the assistant still needs "
                "its contents and re-running the tool would not do"
            ),
            "true": "Keep the full result text",
            "false": "Result can be dropped or truncated",
        },
    }


def batch_calls(
    calls: list[dict[str, Any]], state_tokens: int, max_request_tokens: int
) -> list[list[dict[str, Any]]]:
    budget = max_request_tokens - state_tokens - REQUEST_OVERHEAD
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for call in calls:
        qtok = estimate_tokens(json.dumps(questions_for(call), ensure_ascii=False))
        if current and current_tokens + qtok > budget:
            batches.append(current)
            current = []
            current_tokens = 0
        if not current and qtok > budget:
            raise SystemExit(
                f"state leaves no room for questions (~{state_tokens} of {max_request_tokens} tokens)"
            )
        current.append(call)
        current_tokens += qtok
    if current:
        batches.append(current)
    return batches


def noul_value(answers: dict[str, Any], key: str) -> float:
    a = answers.get(key) or {}
    if isinstance(a, dict):
        if "noul" in a:
            return float(a["noul"])
        # some payloads nest under answer
        if "probability" in a:
            return float(a["probability"])
    return 1.0


def truncated_result(text: str, is_error: bool, head: int) -> str:
    if len(text) <= head + 120:
        return text
    prefix = f"{text[:head]}\n" if head else ""
    err = " (error)" if is_error else ""
    return (
        f"{prefix}[jev-compact truncated {len(text) - head} chars of this tool result{err}; "
        "re-run the tool if needed]"
    )


def apply_decisions(
    messages: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    head_chars: int,
) -> list[dict[str, Any]]:
    by_id = {c["id"]: c for c in calls}
    actions: dict[str, str] = {}
    for d in decisions:
        call = by_id.get(d["id"])
        if call and d["action"] != "keep":
            actions[call["tool_use_id"]] = d["action"]

    kept: list[dict[str, Any]] = []
    for message in messages:
        uses = message.get("toolUses") or []
        results = message.get("toolResults") or []
        touched = any(t["tool_use_id"] in actions for t in uses) or any(
            r["tool_use_id"] in actions for r in results
        )
        if not touched:
            kept.append(message)
            continue

        new_uses = []
        for tool in uses:
            act = actions.get(tool["tool_use_id"])
            if act == "drop_call":
                continue
            if act == "drop_result" and tool.get("text") is not None:
                t = dict(tool)
                t["text"] = truncated_result(
                    tool.get("text") or "", bool(tool.get("isError")), head_chars
                )
                new_uses.append(t)
            else:
                new_uses.append(tool)

        new_results = []
        for result in results:
            act = actions.get(result["tool_use_id"])
            if act == "drop_call":
                continue
            if act == "drop_result":
                r = dict(result)
                r["text"] = truncated_result(
                    result.get("text") or "", bool(result.get("isError")), head_chars
                )
                new_results.append(r)
            else:
                new_results.append(result)

        text = message.get("text") or ""
        if not text.strip() and not new_uses and not new_results:
            continue
        rebuilt: dict[str, Any] = {
            "role": message.get("role"),
            "text": text,
            "toolUses": new_uses,
        }
        if new_results:
            rebuilt["toolResults"] = new_results
        kept.append(rebuilt)
    return kept


def message_chars(message: dict[str, Any]) -> int:
    total = len(message.get("text") or "")
    for tool in message.get("toolUses") or []:
        try:
            total += len(json.dumps(tool.get("input") or {}))
        except TypeError:
            total += 20
    for result in message.get("toolResults") or []:
        total += len(result.get("text") or "")
    return total


def compact_messages(
    openai_messages: list[dict[str, Any]],
    *,
    model: str,
    keep_threshold: float,
    preserve_recent: int,
    max_state_tokens: int,
    max_request_tokens: int,
    truncate_head: int,
    goal: str,
) -> dict[str, Any]:
    started = time.time()
    # Ensure key is present early
    _load_key()

    messages = from_openai(openai_messages)
    calls = collect_tool_calls(messages, preserve_recent)
    candidates = [c for c in calls if not c["pinned"]]
    chars_before = sum(message_chars(m) for m in messages)

    state_tokens = 0
    state_stage = ""
    requests = 0
    answers: dict[str, dict[str, float]] = {}

    if candidates:
        state, state_tokens, state_stage = fit_state(
            messages,
            calls,
            max_state_tokens=max_state_tokens,
            preserve=preserve_recent,
            goal=goal,
        )
        batches = batch_calls(candidates, state_tokens, max_request_tokens)
        requests = len(batches)

        def run_batch(batch: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
            questions: dict[str, Any] = {}
            for call in batch:
                questions.update(questions_for(call))
            # jev_ask expects state as JSON-serializable; pass the state object
            out = jev_ask(state, questions, model)
            ans = out.get("answers") or {}
            local: dict[str, dict[str, float]] = {}
            for call in batch:
                local[call["id"]] = {
                    "keepCall": noul_value(ans, f"call_{call['id']}"),
                    "keepResult": noul_value(ans, f"result_{call['id']}"),
                }
            return local

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(batches)))) as ex:
            for part in ex.map(run_batch, batches):
                answers.update(part)

    decisions = []
    for call in calls:
        ans = answers.get(call["id"], {"keepCall": 1.0, "keepResult": 1.0})
        if call["pinned"]:
            action, reason = "keep", "pinned"
        elif ans["keepResult"] >= keep_threshold:
            action, reason = "keep", "kept"
        elif ans["keepCall"] >= keep_threshold:
            action, reason = "drop_result", "result_dropped"
        else:
            action, reason = "drop_call", "call_dropped"
        decisions.append(
            {
                "id": call["id"],
                "tool": call["tool"],
                "tool_use_id": call["tool_use_id"],
                "keepCall": ans["keepCall"],
                "keepResult": ans["keepResult"],
                "action": action,
                "reason": reason,
            }
        )

    kept = apply_decisions(messages, decisions, calls, truncate_head)
    chars_after = sum(message_chars(m) for m in kept)
    openai_out = to_openai(kept)

    def count(reason: str) -> int:
        return sum(1 for d in decisions if d["reason"] == reason)

    return {
        "messages": openai_out,
        "decisions": decisions,
        "stats": {
            "messagesBefore": len(messages),
            "messagesAfter": len(kept),
            "charsBefore": chars_before,
            "charsAfter": chars_after,
            "reduction": round(
                (chars_before - chars_after) / chars_before if chars_before else 0.0, 4
            ),
            "calls": len(calls),
            "kept": count("kept"),
            "resultsDropped": count("result_dropped"),
            "callsDropped": count("call_dropped"),
            "pinned": count("pinned"),
            "stateTokens": state_tokens,
            "stateStage": state_stage,
            "requests": requests,
            "ms": int((time.time() - started) * 1000),
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Jev tool-history compaction for Hermes")
    p.add_argument("--messages-file", required=True, help="JSON array of OpenAI chat messages")
    p.add_argument("--out", help="Write compacted messages JSON here (default: stdout messages only)")
    p.add_argument("--stats", action="store_true", help="Print stats JSON to stderr")
    p.add_argument("--decisions", action="store_true", help="Include decisions in --out object")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--keep-threshold", type=float, default=0.5)
    p.add_argument("--preserve-recent", type=int, default=6)
    p.add_argument("--max-state-tokens", type=int, default=25_000)
    p.add_argument("--max-request-tokens", type=int, default=30_000)
    p.add_argument("--truncate-head", type=int, default=300)
    p.add_argument("--goal", default="")
    p.add_argument(
        "--min-reduction",
        type=float,
        default=0.0,
        help="Exit 4 if reduction ratio is below this (caller may fall back)",
    )
    args = p.parse_args()

    raw = json.loads(Path(args.messages_file).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "messages" in raw:
        messages = raw["messages"]
    elif isinstance(raw, list):
        messages = raw
    else:
        raise SystemExit("messages file must be a JSON array or {messages:[...]}")

    result = compact_messages(
        messages,
        model=args.model,
        keep_threshold=args.keep_threshold,
        preserve_recent=args.preserve_recent,
        max_state_tokens=args.max_state_tokens,
        max_request_tokens=args.max_request_tokens,
        truncate_head=args.truncate_head,
        goal=args.goal,
    )

    if args.stats:
        print(json.dumps(result["stats"], indent=2), file=sys.stderr)

    payload: Any
    if args.decisions:
        payload = result
    else:
        payload = result["messages"]

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    if args.min_reduction and result["stats"]["reduction"] < args.min_reduction:
        return 4
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JevError as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    except BrokenPipeError:
        sys.exit(0)
