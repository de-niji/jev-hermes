#!/usr/bin/env python3
"""Classify inbox mail with Jev (mail_triage preset) — cheap, typed, one decision per message.

Why: mail triage is mostly if-statements. Doing it in Jev keeps full message bodies OUT of the
main agent context (the real saving) and costs ~$0.00002 per message.

Pipeline
  1. list mail            -> google_api.py gmail search "<query>"
  2. promo/social fastpath -> Gmail CATEGORY_* labels decide 'noise' with NO model call
  3. body head            -> google_api.py gmail get <id> (plain text, truncated)
  4. one Jev call per mail: disposition (choice) + action_needed (noul) + has_deadline (noul)
  5. confidence < --min-confidence -> escalate=true (caller may spend a frontier model on it)

Outputs JSON (and optionally appends CSV) with a coverage + cost summary, so crons can consume
the result instead of re-reading the inbox into context.

Usage
  python3 jev_mail_triage.py --query "in:inbox newer_than:7d" --max 25 --out /tmp/triage.json --brief
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_decide as jd  # noqa: E402  (shares key loading + HTTP path)

GAPI = "/opt/data/skills/productivity/google-workspace/scripts/google_api.py"

# Options live here (code/config), never invented by the model.
# preset "inbox": what should the user do with this mail?
DISPOSITIONS: dict[str, str] = {
    "urgent_reply": "Deadline/Geld/Sicherheit/Blocker heute, oder Anfrage von Chef, Prof, Kunde, Partner",
    "reply": "Direkte Frage oder Bitte an den User, Antwort noetig, aber nicht heute",
    "action_no_reply": "Zahlung, Termin, Anmeldung, Kuendigung, Vertrag, Rechnung: Handlung ohne Antwort",
    "waiting": "Der User hat schon geantwortet, die andere Seite ist am Zug",
    "reference": "Info, Beleg, Rechnung zur Ablage, Vertragsdokument: keine Handlung",
    "noise": (
        "Newsletter, Werbung, Social, sowie automatische System-/Geraetemeldung (Tuer, Sensor, Kamera, "
        "Home Assistant), Bestaetigungs-/Login-/2FA-Code, Sicherheitshinweis zu einem eigenen Login: "
        "kein Handlungsbedarf"
    ),
}

# preset "belege": is this mail worth the expensive PDF/HTML -> register row pipeline?
BELEG_KINDS: dict[str, str] = {
    "beleg": "Rechnung, Invoice, Receipt, Quittung, Kassenbon, Zahlungsbeleg mit Betrag",
    "mahnung": "Mahnung, Zahlungserinnerung, fehlgeschlagene Zahlung, Zahlungsproblem",
    "vertrag": "Vertrag, Abo, Mitgliedschaft, Versicherung, Kuendigung, Vertragsverlaengerung",
    "info": "Newsletter, Werbung, Sicherheits-/Versand-/Konto-Info, AGB, Datenschutz: kein Beleg",
    "unclear": "Aus Betreff und Body-Anfang nicht entscheidbar",
}

PRESETS: dict[str, dict[str, dict]] = {
    "inbox": {
        "disposition": DISPOSITIONS,
        "questions": {
            "disposition": {
                "type": "choice",
                "instructions": (
                    "Classify this email for a personal assistant. Pick the single best bucket. "
                    "Money/deadlines/security from a human or a real vendor beat marketing wording."
                ),
                "criteria": DISPOSITIONS,
            },
            "action_needed": {
                "type": "noul",
                "instructions": "Should this appear on the user focus list this week?",
                "true": "The user must do something (pay, answer, sign, register, cancel)",
                "false": "No action needed from the user",
            },
            "has_deadline": {
                "type": "noul",
                "instructions": "Does the mail contain a concrete date or deadline the user must track?",
                "true": "Explicit date/deadline/Frist present",
                "false": "No explicit date",
            },
        },
    },
    "belege": {
        "disposition": BELEG_KINDS,
        "questions": {
            "disposition": {
                "type": "choice",
                "instructions": (
                    "The user files Belege (invoices/receipts) into a finance register. Does this mail "
                    "contain a document that belongs there, or is it marketing/admin noise? "
                    "A real amount owed or paid beats the sender's marketing wording. "
                    "'info' includes security alerts and account notifications, even from real vendors."
                ),
                "criteria": BELEG_KINDS,
            },
            "money_relevant": {
                "type": "noul",
                "instructions": "Does the mail state a concrete amount, or confirm/announce a payment?",
                "true": "Betrag, Summe, Amount, gezahlter/offener Betrag erkennbar",
                "false": "Kein Geldbetrag",
            },
            "has_attachment": {
                "type": "noul",
                "instructions": "Does the mail appear to carry a PDF/invoice attachment (or inline invoice table)?",
                "true": "Anhang oder eingebettete Rechnung vorhanden",
                "false": "Kein Anhang",
            },
        },
    },
}


# Gmail labels that make a model call pointless.
NOISE_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")


def run_json(cmd: list[str]) -> object:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout)


def list_mail(query: str, max_n: int) -> list[dict]:
    out = run_json(["python3", GAPI, "gmail", "search", query, "--max", str(max_n)])
    return out if isinstance(out, list) else []


def body_head(msg_id: str, chars: int) -> str:
    try:
        m = run_json(["python3", GAPI, "gmail", "get", msg_id])
    except Exception:
        return ""
    body = (m.get("body") or "") if isinstance(m, dict) else ""
    body = " ".join(body.split())
    return body[:chars]


def _noul(answers: dict, key: str) -> tuple[float | None, bool | None]:
    """noul answers come back as a probability (0..1), not a boolean. Threshold at 0.5."""
    a = answers.get(key) or {}
    if not isinstance(a, dict):
        return None, None
    raw = a.get("noul")
    if raw is None:
        return None, None
    try:
        p = float(raw)
    except (TypeError, ValueError):
        return None, None
    return p, p >= 0.5


def answers_brief(answers: dict) -> tuple[str, float, dict[str, dict]]:
    """Return (choice, confidence, signals) where signals[key] = {"p": float|None, "yes": bool}."""
    disp, conf = "unknown", 0.0
    a = answers.get("disposition") or {}
    if isinstance(a, dict):
        disp = str(a.get("choice") or "unknown")
        conf = float(a.get("confidence") or 0.0)
    signals: dict[str, dict] = {}
    for key, val in answers.items():
        if key == "disposition" or not isinstance(val, dict):
            continue
        if val.get("type") != "noul":
            continue
        p, yes = _noul(answers, key)
        signals[key] = {"p": p, "yes": yes}
    return disp, conf, signals


def needs_attention(row: dict, preset: str) -> bool:
    sig = row.get("signals") or {}
    if preset == "belege":
        if row["disposition"] in ("beleg", "mahnung", "vertrag"):
            return True
        money = (sig.get("money_relevant") or {}).get("yes")
        return bool(money) and row["disposition"] != "info"
    return bool((sig.get("action_needed") or {}).get("yes"))


def main() -> int:
    p = argparse.ArgumentParser(description="Jev mail triage")
    p.add_argument("--preset", choices=sorted(PRESETS), default="inbox")
    p.add_argument("--query", default="in:inbox newer_than:7d")
    p.add_argument("--max", type=int, default=25)
    p.add_argument("--body-chars", type=int, default=600)
    p.add_argument("--no-body", action="store_true", help="subject+snippet only (fewer calls)")
    p.add_argument("--min-confidence", type=float, default=0.5)
    p.add_argument("--model", default=jd.DEFAULT_MODEL)
    p.add_argument("--no-fastpath", action="store_true", help="classify promo mail too")
    p.add_argument("--out", default=None)
    p.add_argument("--csv", default=None, help="append per-mail rows to this CSV")
    p.add_argument("--brief", action="store_true")
    args = p.parse_args()

    preset = PRESETS[args.preset]
    qs = preset["questions"]
    fastpath_label = "noise" if args.preset == "inbox" else "info"
    if args.out is None:
        args.out = f"/tmp/jev_mail_triage/last_{args.preset}.json"

    t0 = time.time()
    mails = list_mail(args.query, args.max)
    rows: list[dict] = []
    cost_total = 0.0
    errors: list[str] = []

    for m in mails:
        labels = m.get("labels") or []
        row = {
            "id": m.get("id"),
            "threadId": m.get("threadId"),
            "from": m.get("from"),
            "subject": m.get("subject"),
            "date": m.get("date"),
            "labels": labels,
            "disposition": None,
            "confidence": None,
            "signals": {},
            "escalate": False,
            "source": None,
            "cost": 0.0,
        }

        if not args.no_fastpath and any(l in labels for l in NOISE_LABELS):
            row.update(disposition=fastpath_label, confidence=1.0,
                       source="label_fastpath")
            rows.append(row)
            continue

        state = {
            "from": m.get("from"),
            "subject": m.get("subject"),
            "labels": labels,
            "date": m.get("date"),
            "body": "" if args.no_body else body_head(str(m.get("id")), args.body_chars),
        }
        if not state["body"]:
            state["snippet"] = (m.get("snippet") or "")[:400]

        try:
            out = jd.decide(state, qs, args.model)
        except SystemExit as e:
            errors.append(f"{m.get('id')}: {e}")
            rows.append(row)
            continue

        disp, conf, signals = answers_brief(out.get("answers") or {})
        cost = float((out.get("usage") or {}).get("cost") or 0.0)
        cost_total += cost
        row.update(disposition=disp, confidence=conf, signals=signals,
                   escalate=conf < args.min_confidence, source="jev", cost=cost)
        rows.append(row)

    counts: dict[str, int] = {}
    for r in rows:
        key = r["disposition"] or "unknown"
        counts[key] = counts.get(key, 0) + 1

    result = {
        "preset": args.preset,
        "query": args.query,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": args.model,
        "mail_count": len(mails),
        "classified": sum(1 for r in rows if r["disposition"]),
        "counts": counts,
        "needs_attention": [r for r in rows if needs_attention(r, args.preset)],
        "escalate": [r["id"] for r in rows if r["escalate"]],
        "cost_usd": round(cost_total, 8),
        "cost_per_mail_usd": round(cost_total / len(rows), 8) if rows else 0.0,
        "seconds": round(time.time() - t0, 1),
        "errors": errors,
        "rows": rows,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_cols = ["generated", "preset", "id", "date", "from", "subject", "disposition",
                "confidence", "signals", "escalate", "source", "cost"]
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        new = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=csv_cols)
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({"generated": result["generated"], "preset": args.preset,
                            **{k: (json.dumps(r[k], ensure_ascii=False) if k == "signals" else r[k])
                               for k in ("id", "date", "from", "subject", "disposition",
                                         "confidence", "signals", "escalate", "source", "cost")}})

    if args.brief:
        print(f"preset={args.preset} mails={result['mail_count']} classified={result['classified']} "
              f"cost=${result['cost_usd']:.6f} per_mail=${result['cost_per_mail_usd']:.8f} "
              f"{result['seconds']}s out={out_path}")
        print("counts=" + " ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
        for r in result["needs_attention"]:
            flag = "!" if r["escalate"] else " "
            sig = " ".join(f"{k}={v['p']}" for k, v in (r["signals"] or {}).items() if v["p"] is not None)
            print(f"{flag}{r['disposition']:8s} {r['confidence']:<5} {str(r['from'])[:26]:28s} "
                  f"{str(r['subject'])[:44]:46s} {sig}")
        if result["escalate"]:
            print("escalate=" + ",".join(result["escalate"]))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
