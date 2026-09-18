# jev-hermes

Tiny Hermes skill that calls **TypeSafe Jev** (System One) through OpenRouter’s Decisions API.

Jev does **not** chat or write code. It answers typed questions (`noul` / `choice` / `score`) about a `state` and returns calibrated probabilities. Use it to **route or gate** expensive Hermes agent turns.

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
  --preset intent

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

## Hermes usage

See `SKILL.md`. Pattern: call Jev first on short user text; only start the full agent loop when `intent=complex` or confidence is low.

## Privacy / ZDR

Confirm on OpenRouter whether `typesafe/jev-*` honors `data_collection: deny` for your account before sending private mail/calendar text. If not, keep Jev for non-sensitive routing only, or call TypeSafe directly under their DPA.

## License

MIT
