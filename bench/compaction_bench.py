#!/usr/bin/env python3
"""Measure the Jev context engine's pruning on labelled synthetic sessions.

For every tool output the sessions record whether it is still needed at the end. The benchmark
runs the engine's prune step (hermes_context.jev_stub) with real Jev and reports:

  saved      estimated tokens removed
  false drop needed outputs Jev stubbed  <- the number that matters: the agent loses information
  kept stale stale outputs Jev kept      <- missed savings, harmless

    OPENROUTER_API_KEY=... python3 bench/compaction_bench.py
    python3 bench/compaction_bench.py --oracle     # no key: checks the harness with perfect answers

The protected tail is 0 by default so every output is judged (the engine itself never touches
the last ``compression.protect_last_n`` messages, which only makes it safer than measured here).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_context as hc  # noqa: E402
from hermes_context import jd, jev_compact  # noqa: E402
from sessions import SESSIONS  # noqa: E402


def tokens(messages: list[dict]) -> int:
    return jev_compact.estimate_tokens(json.dumps(messages, ensure_ascii=False))


def oracle(labels: dict[str, str]):
    def score(messages, ids):
        return {cid: {"keepCall": 1.0, "keepResult": 0.9 if labels[cid] == "needed" else 0.1} for cid in ids}
    return score


def jev(protect_tail: int, timeout: float):
    def score(messages, ids):
        return jev_compact.score_calls(messages, ids, preserve=protect_tail, timeout=timeout)
    return score


def run(name: str, builder, score, *, keep_threshold: float, protect_tail: int, min_chars: int) -> dict:
    messages, labels = builder.messages, builder.labels
    boundary = len(messages) - protect_tail
    out, _ = hc.jev_stub(messages, boundary, score, keep_threshold=keep_threshold, min_chars=min_chars)
    stubbed = {m["tool_call_id"] for m in out if m.get("role") == "tool"
               and isinstance(m.get("content"), str) and m["content"].startswith(hc.STUB_PREFIX)}
    needed = {c for c, lab in labels.items() if lab == "needed"}
    stale = {c for c, lab in labels.items() if lab == "stale"}
    before, after = tokens(messages), tokens(out)
    return {
        "session": name, "outputs": len(labels), "needed": len(needed), "stale": len(stale),
        "stubbed": len(stubbed), "false_drops": sorted(stubbed & needed), "kept_stale": sorted(stale - stubbed),
        "tokens_before": before, "tokens_after": after,
        "saved_pct": round(100 * (before - after) / before, 1) if before else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--oracle", action="store_true", help="perfect answers from the labels (no API key)")
    p.add_argument("--keep-threshold", type=float, default=hc.DEFAULTS["keep_threshold"])
    p.add_argument("--protect-tail", type=int, default=0)
    p.add_argument("--min-chars", type=int, default=200)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--json", help="also write the results here")
    args = p.parse_args(argv)

    if not args.oracle:
        try:
            jd._load_key()
        except jd.JevError as e:
            print(f"{e}. Set OPENROUTER_API_KEY, or run with --oracle to check the harness.", file=sys.stderr)
            return 2

    rows = []
    for name, build in SESSIONS.items():
        b = build()
        score = oracle(b.labels) if args.oracle else jev(args.protect_tail, args.timeout)
        try:
            rows.append(run(name, b, score, keep_threshold=args.keep_threshold,
                            protect_tail=args.protect_tail, min_chars=args.min_chars))
        except jd.JevError as e:
            print(f"{name}: Jev error: {e}", file=sys.stderr)
            return 2

    print(f"{'session':10} {'outputs':>7} {'stubbed':>7} {'false drop':>10} {'kept stale':>10} {'tokens':>15} {'saved':>6}")
    for r in rows:
        print(f"{r['session']:10} {r['outputs']:>7} {r['stubbed']:>7} {len(r['false_drops']):>10} "
              f"{len(r['kept_stale']):>10} {r['tokens_before']:>7}->{r['tokens_after']:<7} {r['saved_pct']:>5}%")
    total = {k: sum(r[k] for r in rows) for k in ("outputs", "needed", "stale", "stubbed", "tokens_before", "tokens_after")}
    false_drops = sum(len(r["false_drops"]) for r in rows)
    kept_stale = sum(len(r["kept_stale"]) for r in rows)
    saved = round(100 * (total["tokens_before"] - total["tokens_after"]) / total["tokens_before"], 1)
    print(f"\ntotal: {total['stubbed']}/{total['stale']} stale outputs stubbed, "
          f"{false_drops}/{total['needed']} needed outputs wrongly stubbed, {kept_stale} stale kept, "
          f"{saved}% tokens saved ({'oracle' if args.oracle else 'Jev'}, keep_threshold={args.keep_threshold})")
    for r in rows:
        if r["false_drops"]:
            print(f"  false drops in {r['session']}: {', '.join(r['false_drops'])}")
    if args.json:
        Path(args.json).write_text(json.dumps({"args": vars(args), "rows": rows}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
