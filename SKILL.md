---
name: jev
description: "Fast typed decisions via TypeSafe Jev on OpenRouter (intent/approval/mail triage). Use before expensive agent loops."
version: 0.2.0
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

- Classify user intent before a long Hermes turn
- Pre-filter risky shell / approval cases
- Triage mail subjects (invoice / deadline / ignore)
- Gate crons (“is a full brief worth it?”)
- When context is large: compact tool noise, keep user/assistant text verbatim

Do **not** use for writing replies, code, or multi-step tool plans.

## Ops rule: route first

Before memory *exploration* or multi-tool tours, run one flat intent call (`--brief`). Cost is typically ~$0.00002.

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
