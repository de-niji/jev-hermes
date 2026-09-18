---
name: jev
description: "Fast typed decisions via TypeSafe Jev on OpenRouter (intent/approval/mail triage/compaction). Use to delete LLM calls that are just if-statements."
version: 0.2.1
author: jev-hermes contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [jev, typesafe, routing, openrouter, cost, honcho, compaction]
    related_skills: []
---

# Jev (TypeSafe System One)

Call **Jev** for cheap, typed decisions. It does **not** generate chat or run tools.

OpenRouter: `POST /api/alpha/decisions` · model `typesafe/jev-1.13`

**Not a memory replacement.** Keep Honcho (or any memory provider) fully on. Jev only decides *whether this turn* needs a memory search / full agent loop.

## Two jobs

1. **Intent routing** (`jev_decide.py`) — classify the next user message before a full agent tour  
2. **Tool-history compaction** (`jev_compact.py`) — drop/truncate stale tool calls/results without LLM summarization (inspired by [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction))

## When to use

Replace frontier-model “picks” with typed Jev questions:

- which route / tools next (`intent`)
- is this shell risky (`approval`)
- is this mail spam / invoice / deadline (`mail_triage`)
- is this tool call/result still needed (`compact`)
- Gate crons (“is a full brief worth it?”)

Do **not** use for writing replies, code, or multi-step tool plans.

## Ops rule: put Jev in the loop

1. **Router** — before memory exploration / multi-tool tours: `--preset intent --brief` (~$0.00002).
2. **Gate** — before irreversible tools: `--preset approval` (or custom `choice` from a code-built allowlist).
3. **Compact** — when context is fat with old tool dumps: `jev_compact.py` (verbatim text, drop dead tools).
4. **Batch** questions in one call; **threshold confidence** (&lt;0.5 escalate, ≥0.85 for irreversible). Never invent `choice` options in the prompt — build them in code/config.

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
- Keep `state` small. For `mail_triage`: subject + snippet only.
- Never paste API keys into chat.
- Options come from code/config, not from the model inventing candidates.

## Commands — intent

```bash
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state "<user message>" --preset intent --brief
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state '{"cmd":"<command>"}' --preset approval --brief
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state-file /tmp/mail.json --preset mail_triage --brief
```

`--brief` → one line. `--pretty` → full JSON.

## Commands — compact

Input: JSON array of OpenAI-style chat messages (Hermes transcript dump).

```bash
python3 /opt/data/skills/devops/jev/scripts/jev_compact.py \
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

- `choice` options must live under `criteria` as a **record** `{option: description}`.
- `score` uses `criteria` as an **array** of level labels.
- Flat option keys at question level → `HTTP 400` on `questions.*.criteria`.
