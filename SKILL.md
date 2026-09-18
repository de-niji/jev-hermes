---
name: jev
description: "Fast typed decisions via TypeSafe Jev on OpenRouter (intent/approval/mail triage). Use before expensive agent loops."
version: 0.1.0
author: jev-hermes contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [jev, typesafe, routing, openrouter, cost]
    related_skills: []
---

# Jev (TypeSafe System One)

Call **Jev** for cheap, typed decisions. It does **not** generate chat or run tools.

OpenRouter: `POST /api/alpha/decisions` · model `typesafe/jev-1.13`

## When to use

- Classify user intent before a long Hermes turn
- Pre-filter risky shell / approval cases
- Triage mail subjects (invoice / deadline / ignore)
- Gate crons (“is a full brief worth it?”)

Do **not** use for writing replies, code, or multi-step tool plans.

## Rules

- Flat commands only (no `JEV=…; $JEV` — Tirith).
- Keep `state` small: user text + short facts, not full calendars.
- If confidence is low or intent is `complex`, continue with the normal agent.
- Never paste API keys into chat.

## Commands

```bash
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state "<user message>" --preset intent
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state '{"cmd":"<command>"}' --preset approval
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state-file /tmp/mail.json --preset mail_triage
```

## Env

- `OPENROUTER_API_KEY` (required) — same key Hermes already uses is fine
- `JEV_MODEL` optional, default `typesafe/jev-1.13`

## Output

JSON on stdout with `answers`, `model`, `usage`. Use fields in code / next step; do not narrate the whole JSON to the user unless asked.
