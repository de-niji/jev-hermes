# jev-hermes

Tiny Hermes skill that calls **TypeSafe Jev** (System One) through OpenRouter’s Decisions API.

Jev does **not** chat or write code. It answers typed questions (`noul` / `choice` / `score`) about a `state` and returns calibrated probabilities. Use it to **route or gate** expensive Hermes agent turns.

## Architecture: router, not memory

Jev does **not** replace a memory system (e.g. Honcho). Keep memory fully enabled.

| Layer | Role |
|-------|------|
| **Jev** | Cheap intent gate before a turn |
| **Config files** | Stable IDs and how-tos (calendar ids, allowlists) |
| **Memory (Honcho, etc.)** | People, preferences, projects, “what did we decide” |
| **Main LLM** | Reasoning + tools when the route needs it |

**Token savings** come from skipping long tool/memory *tours* on clear `calendar` / `mail` / `status` asks — not from turning memory off.

- `route=calendar|mail|status` → config + flat tools only; **no** memory search spam that turn  
- `route=complex` / people / prefs / “what did we…” → memory + normal agent as usual  
- Memory providers still **write** in the background either way  

| | |
|---|---|
| OpenRouter model | `typesafe/jev-1.13` (override with `JEV_MODEL`) |
| Endpoint | `POST https://openrouter.ai/api/alpha/decisions` |
| Price (approx.) | ~$0.042 / M input · **$0** output |

## Install on Hermes

```bash
git clone https://github.com/de-niji/jev-hermes.git ~/jev-hermes
# or update: cd ~/jev-hermes && git pull

# copy into the Hermes skills tree (adjust if your HERMES_HOME differs):
docker cp ~/jev-hermes/. hermes:/opt/data/skills/devops/jev/
docker exec -u 0 hermes chown -R 10000:1000 /opt/data/skills/devops/jev

# OPENROUTER_API_KEY in the Hermes env / .env
# optional: JEV_MODEL=typesafe/jev-1.13
```

## CLI

```bash
# from skill dir
python3 scripts/jev_decide.py \
  --state "What's on my calendar tomorrow?" \
  --preset intent --brief

python3 scripts/jev_decide.py \
  --state '{"cmd":"rm -rf /","why":"cleanup"}' \
  --preset approval

python3 scripts/jev_decide.py \
  --state-file examples/mail_sample.json \
  --preset mail_triage

# custom questions (JSON)
python3 scripts/jev_decide.py \
  --state "Please send the invoice by Friday." \
  --questions-file examples/custom_questions.json
```

Exit codes: `0` ok · `2` API/config error · `3` low confidence (when `--min-confidence` set)

## Presets

| Preset | Use |
|--------|-----|
| `intent` | Route: calendar / mail / status / research / complex |
| `approval` | Is this shell command risky enough to escalate? |
| `mail_triage` | Invoice / deadline / contract / ignore |

## Pitfalls

- `choice` options must be under `criteria` as a **record** (option → description). Arrays in `criteria` are for `score` only. Wrong shapes return HTTP 400.

## Hermes usage

See `SKILL.md`. Pattern: call Jev first on short user text; only start the full agent loop when `intent=complex` or confidence is low.

## Privacy / ZDR

If your OpenRouter account routes `typesafe/jev-*` under zero-data-retention, Jev is fine for the same traffic class as your other ZDR models. Otherwise keep private mail/calendar text out of `state`, or use TypeSafe under their DPA.

## License

MIT
