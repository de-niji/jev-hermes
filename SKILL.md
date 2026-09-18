---
name: jev
description: "Fast typed decisions via TypeSafe Jev on OpenRouter (intent/approval/mail triage). Use before expensive agent loops."
version: 0.1.1
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

## Ops rule: route first

Before memory exploration or multi-tool tours, run one flat intent call (`--brief`). Cost is typically ~$0.00002.

| `route` | then |
|---|---|
| `calendar` | only flat calendar list calls |
| `mail` | only flat gmail search/get |
| `status` | only uptime/status CLI |
| `research` | short web path (1–2 searches) |
| `complex` / confidence &lt; 0.7 | full agent loop |

`needs_tools` &lt; 0.5 and clearly answerable → reply directly, no tools.

## Rules

- Flat commands only (no `JEV=…; $JEV` — Tirith).
- Keep `state` small. For `mail_triage`: subject + snippet only.
- Never paste API keys into chat.

## Commands

```bash
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state "<user message>" --preset intent --brief
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state '{"cmd":"<command>"}' --preset approval --brief
python3 /opt/data/skills/devops/jev/scripts/jev_decide.py --state-file /tmp/mail.json --preset mail_triage --brief
```

`--brief` → one line. `--pretty` → full JSON.

## Pitfalls

- `choice` options must live under `criteria` as a **record** `{option: description}`.
- `score` uses `criteria` as an **array** of level labels.
- Flat option keys at question level → `HTTP 400` on `questions.*.criteria`.
