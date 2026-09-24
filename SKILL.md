---
name: jev
description: "Fast typed decisions via TypeSafe Jev on OpenRouter (intent/approval/mail triage/compaction). Use to delete LLM calls that are just if-statements."
version: 0.5.0
author: jev-hermes contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [jev, typesafe, routing, openrouter, cost, honcho, compaction, mail]
    related_skills: []
---

# Jev (TypeSafe System One)

Call **Jev** for cheap, typed decisions. It does **not** generate chat or run tools.

OpenRouter: `POST /api/alpha/decisions` · model `typesafe/jev-1.13`

**Not a memory replacement.** Keep Honcho (or any memory provider) fully on. Jev only decides *whether this turn* needs a memory search / full agent loop.

**Plugin tools first.** When the `jev` plugin is enabled (`hermes plugins install de-niji/jev-hermes --enable`), call the `jev_decide` and `jev_mail_triage` tools directly. The commands below are the fallback when only this skill is installed.

Files (paths relative to this skill; `${HERMES_SKILL_DIR}` in the commands):
`scripts/jev_decide.py` · `scripts/jev_mail_triage.py` · `scripts/jev_compact.py` · `examples/mail_sample.json` · `examples/mails_sample.json` · `examples/custom_questions.json` · `examples/compact_sample.json`

## Three jobs

1. **Intent routing** (`jev_decide.py`) — classify the next user message before a full agent tour  
2. **Tool-history compaction** (`jev_compact.py`) — drop/truncate stale tool calls/results without LLM summarization (inspired by [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction))  
3. **Mail classification** (`jev_mail_triage.py`) — bucket inbox mail before any frontier model sees it

## When to use

Replace frontier-model “picks” with typed Jev questions:

- which route / tools next (`intent`)
- is this shell risky (`approval`)
- is this mail spam / invoice / deadline (`mail_triage` / `jev_mail_triage.py`)
- is this tool call/result still needed (`compact`)
- Gate crons (“is a full brief worth it?”)

Do **not** use for writing replies, code, or multi-step tool plans.

## Ops rule: put Jev in the loop

1. **Router** — before memory exploration / multi-tool tours: `--preset intent --brief` (~$0.00002).
2. **Gate** — before irreversible tools: `--preset approval` (or custom `choice` from a code-built allowlist).
3. **Mail** — classify with `jev_mail_triage.py` so bodies never enter the main agent context.
4. **Compact** — when context is fat with old tool dumps: `jev_compact.py` (verbatim text, drop dead tools).
5. **Batch** questions in one call; **threshold confidence** (&lt;0.5 escalate, ≥0.85 for irreversible). Never invent `choice` options in the prompt — build them in code/config.

| `route` | then |
|---|---|
| `calendar` | config files for IDs OK; only flat calendar list calls; **no** memory search |
| `mail` | only flat gmail search/get; **no** memory search |
| `status` | only uptime/status CLI; **no** memory search |
| `research` | short web path (1–2 searches) |
| `complex` / confidence &lt; 0.7 / people-prefs | memory + full agent loop |

`needs_tools` &lt; 0.5 and clearly answerable → reply directly, no tools.

Memory still **persists** messages in the background on every turn.

## Rules

- Flat commands only (no `JEV=…; $JEV` — Tirith).
- Keep `state` small. For single-mail decide: subject + snippet only.
- Never paste API keys into chat.
- Options come from code/config, not from the model inventing candidates.

## Commands — intent

```bash
python3 ${HERMES_SKILL_DIR}/scripts/jev_decide.py --state "<user message>" --preset intent --brief
python3 ${HERMES_SKILL_DIR}/scripts/jev_decide.py --state '{"cmd":"<command>"}' --preset approval --brief
python3 ${HERMES_SKILL_DIR}/scripts/jev_decide.py --state-file /tmp/mail.json --preset mail_triage --brief
```

`--brief` → one line. `--pretty` → full JSON.

## Commands — mail triage

One decision per message, options fixed in code, Gmail promo/social labels short-circuit to `noise`/`info` with **no model call**. Bodies are fetched and truncated locally so they never enter the main agent context.

Requires Hermes Google Workspace skill (`google_api.py`) for Gmail list/get (override the path with `JEV_GAPI`). Mail from any other source: `--input mails.json` (JSON array, `-` = stdin), no Gmail needed.

Two presets: `inbox` (urgent_reply / reply / action_no_reply / waiting / reference / noise) and `receipts` (receipt / payment_issue / contract / info / unclear — gate before an expensive finance/PDF pipeline).

```bash
python3 ${HERMES_SKILL_DIR}/scripts/jev_mail_triage.py \
  --preset inbox --query "newer_than:2d -in:chats" --max 20 \
  --out /tmp/jev_mail_triage/last_inbox.json --brief

python3 ${HERMES_SKILL_DIR}/scripts/jev_mail_triage.py \
  --preset receipts --query "newer_than:14d -in:chats" --max 30 --brief
```

- Output JSON: `rows[]`, `needs_attention[]`, `escalate[]` (conf &lt; `--min-confidence`, default 0.5), `counts`, `cost_usd`.
- `source=label_fastpath` rows cost nothing; `source=jev` rows ~$0.00002 each; `source=error` rows (Jev call failed) are always in `escalate[]`.
- `needs_attention[]` always includes action dispositions (`urgent_reply` / `reply` / `action_no_reply`, or `receipt` / `payment_issue` / `contract`), plus rows whose noul signals say so.
- Callers should only escalate `escalate[]` ids to a frontier model — that is the cheap hybrid.
- `--preset receipts` also catches receipts with **no PDF attachment** (inline invoices) that a `has:attachment filename:pdf` scan misses.

### Measured A/B (scrubbed)

Frozen 20-mail snapshot, subject + 600 chars body (~2.3k tokens of mail text). Jev vs main chat model (per-mail and one batch). Aggregate only — no mail content in this repo.

| Arm | Wall clock | Model cost | vs Jev |
|---|---|---|---|
| Jev (20 decisions) | ~8.6 s | ~$0.0007 | — |
| Main model, 1 call/mail | ~73 s | ~$0.013 | ~18× cost, ~8× slower |
| Main model, 1 batch | ~65 s | ~$0.0013 | ~1.9× cost, ~7× slower |

Real win: **zero mail text in the agent context**, plus wall clock. Criteria must name concrete failure modes (door/sensor/Home Assistant alerts, login/2FA codes) — vague “automated notification” pushed those into action buckets above the 0.5 confidence gate.

## Commands — compact

Input: JSON array of OpenAI-style chat messages (Hermes transcript dump).

```bash
python3 ${HERMES_SKILL_DIR}/scripts/jev_compact.py \
  --messages-file /tmp/messages.json \
  --out /tmp/messages.compact.json \
  --stats --decisions \
  --min-reduction 0.25
```

- Exit `0` = compacted OK  
- Exit `4` = reduction below `--min-reduction` (caller should keep original / fall back to Hermes summary)  
- Pins first message + newest `--preserve-recent` (default 6)  
- Per old tool pair: keep call? keep full result? → keep / truncate result / drop both  

## Pitfalls

- Criteria text must name the concrete failure mode ("automated device alert — door, sensor, Home Assistant", "login/2FA code"), not a generic label like "automated notification".
- `choice` options must live under `criteria` as a **record** `{option: description}`.
- `score` uses `criteria` as an **array** of level labels.
- `noul` answers come back as a **probability float** (`{"type":"noul","noul":0.25}`), not a boolean — threshold at 0.5 explicitly.
- Flat option keys at question level → `HTTP 400` on `questions.*.criteria`.
