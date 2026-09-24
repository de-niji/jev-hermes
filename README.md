# jev-hermes

**TypeSafe Jev for [Hermes Agent](https://github.com/NousResearch/hermes-agent).** A drop-in Hermes skill that hands cheap, typed decisions to Jev (System One) through OpenRouter’s Decisions API — so the expensive model only runs when it has to.

Jev does **not** chat or write code. It answers typed questions (`noul` / `choice` / `score`) about a `state` and returns calibrated probabilities. In Hermes it **routes** turns before the agent loop, **gates** risky commands, **compacts** old tool output and **triages** mail.

## Install in Hermes

```bash
git clone https://github.com/de-niji/jev-hermes.git ~/jev-hermes
# or update: cd ~/jev-hermes && git pull

# copy into the Hermes skills tree (adjust if your HERMES_HOME differs):
docker cp ~/jev-hermes/. hermes:/opt/data/skills/devops/jev/
docker exec -u 0 hermes chown -R 10000:1000 /opt/data/skills/devops/jev

# OPENROUTER_API_KEY in the Hermes env / .env
# optional: JEV_MODEL=typesafe/jev-1.13
```

The key is also read from `~/.hermes/.env` and `/opt/data/.env` when it is not in the environment.

## How Hermes uses it

`SKILL.md` is the skill manifest Hermes loads; it tells the agent when to call which script. Pattern: call Jev first on short user text; only start the full agent loop when `intent=complex` or confidence is low.

- `route=calendar|mail|status` → config + flat tools only; **no** memory search spam that turn
- `route=complex` / people / prefs / “what did we…” → memory + normal agent as usual
- Memory providers still **write** in the background either way

## Architecture: router, not memory

Jev does **not** replace a memory system (e.g. Honcho). Keep memory fully enabled.

| Layer | Role |
|-------|------|
| **Jev intent** | Cheap gate before a turn |
| **Jev mail triage** | Bucket inbox / finance candidates before the agent reads bodies |
| **Jev compact** | Drop/truncate stale **tool** noise (text stays verbatim) |
| **Config files** | Stable IDs and how-tos (calendar ids, allowlists) |
| **Memory (Honcho, etc.)** | People, preferences, projects, “what did we decide” |
| **Main LLM** | Reasoning + tools when the route needs it |

**The 100x is not “swap the LLM for Jev.”** It is deleting calls that never needed a language model — which-tool-next, is-this-spam, is-this-chunk-relevant, does-this-need-a-human, is-this-diff-risky. Those are if-statements outsourced to a frontier model.

| | |
|---|---|
| OpenRouter model | `typesafe/jev-1.13` (override with `JEV_MODEL`) |
| Endpoint | `POST https://openrouter.ai/api/alpha/decisions` |
| Price (approx.) | ~$0.042 / M input · **$0** output |

## Playbook (where to put it)

1. **Typed question** — `choice` (≤255 options), `score` (2–10 levels), `noul` (0–1). Not free-form.
2. **Batch** — many questions in one call run in parallel; output tokens are free. Ask everything you might need.
3. **Threshold on confidence, not the answer** — &lt;0.5 escalate to big model/human; ≥0.85 before anything irreversible.
4. **Never invent options** — build the candidate list in code (tools, DOM, retriever, allowlists), then let Jev pick.
5. **In the loop, not beside it** — router (cheap model / skip tour) → gate (before tool runs) → judge (after output). Compaction is the cheap win tonight.
6. **Compaction first for bills** — score tool pairs, drop dead ones, keep survivors **verbatim** (no lossy summary).

Mapping in this repo: **router** = `intent` preset · **gate** = `approval` / `jev_mail_triage.py` · **compact** = `jev_compact.py`. Judge-after-tool is not covered yet.

Honest limits: text only (no images/audio). Wins on narrow, well-specified decisions — most of what an agent does all day — not on broad chat benchmarks.

## CLI

```bash
# from the repo root
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

Exit codes: `0` ok · `2` API/config error (HTTP, network, missing key) · `3` low confidence (when `--min-confidence` set)

## Presets

| Preset | Use |
|--------|-----|
| `intent` | Route: calendar / mail / status / research / complex |
| `approval` | Is this shell command risky enough to escalate? |
| `mail_triage` | Invoice / deadline / contract / ignore / other |

## Compaction (tool history)

Inspired by [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction). Does **not** summarize: user/assistant text stays verbatim; only tool calls/results may be dropped or truncated.

```bash
python3 scripts/jev_compact.py \
  --messages-file examples/compact_sample.json \
  --stats --decisions \
  --out /tmp/out.json
```

Input: a JSON array of OpenAI-style chat messages (or `{"messages": [...]}`). Use when a Hermes session is long and full of old tool dumps. If `--min-reduction` is not met, exit code `4` — keep the original transcript or fall back to Hermes' built-in summary.

## Mail triage

`scripts/jev_mail_triage.py` makes one Jev decision per mail. Options are fixed in code (`inbox` / `receipts` presets). Gmail promo/social/forum labels short-circuit with no model call. Bodies are truncated locally so they never enter the main agent context.

**Gmail (default)** — mail is listed and fetched through the Hermes Google Workspace skill (`google_api.py gmail search` / `gmail get`). Point `JEV_GAPI` at a different script if yours lives elsewhere.

```bash
python3 scripts/jev_mail_triage.py --preset inbox --query "newer_than:2d -in:chats" --max 20 --brief
python3 scripts/jev_mail_triage.py --preset receipts --query "newer_than:14d -in:chats" --max 30 --brief
```

**Other mail sources** — pass a JSON array (file or `-` for stdin). Only `subject` is really needed; `body` (or `snippet`) and `labels` help:

```json
[{"id": "m1", "from": "billing@example.com", "subject": "Invoice No. 10442", "date": "2026-09-20",
  "labels": ["INBOX"], "body": "Please transfer 49.90 EUR by 2026-09-30."}]
```

```bash
python3 scripts/jev_mail_triage.py --input examples/mails_sample.json --brief
my_mail_exporter | python3 scripts/jev_mail_triage.py --input - --preset receipts --brief
```

Output JSON (default `/tmp/jev_mail_triage/last_<preset>.json`): `rows[]`, `needs_attention[]`, `escalate[]` (confidence below `--min-confidence`, default 0.5, or a failed Jev call), `counts`, `cost_usd`. Mails whose Jev call fails are kept with `source=error`, never dropped. Exit `2` on a missing API key. **Do not commit real inbox dumps or mail snapshots.**

Aggregate A/B (20-mail frozen snapshot): Jev ~8.6 s / ~$0.0007 vs per-mail main model ~73 s / ~$0.013. See `SKILL.md` for pitfalls (criteria must name concrete failure modes).

## Pitfalls

- `choice` options must be under `criteria` as a **record** (option → description). Arrays in `criteria` are for `score` only. Wrong shapes return HTTP 400.
- Mail criteria: name concrete noise modes (device alerts, 2FA codes), not vague “automated notification”.

## Hermes host tips: pairing with Honcho

Jev does not configure Honcho. On a Hermes host, the big token cost is usually **uncapped every-turn memory inject**, not the Jev gate.

Recommended host knobs (in your local `honcho.json` — **do not commit personal peer/workspace names**):

| Knob | Suggested | Why |
|------|-----------|-----|
| `contextTokens` | `1500`–`2500` | Cap auto-injected context |
| `contextCadence` | `4`–`8` | Refresh base context less often |
| `injection.sessionStart` | `["summary","peerCard"]` | Skip heavy representation blocks |
| `dialecticCadence` | `≥8` | Keep dialectic sparse |
| `dialecticReasoningLevel` | `minimal` | Cheap dialectic |

Keep `recallMode: hybrid` if you still want tools on `complex` turns. Live PA routing notes stay on the host workspace, not in this repo.

## Use without Hermes

The scripts are plain Python ≥ 3.9 (standard library only), so they also run outside Hermes — from a cron job, another agent or a shell. You need an [OpenRouter](https://openrouter.ai) API key.

```bash
git clone https://github.com/de-niji/jev-hermes.git && cd jev-hermes
export OPENROUTER_API_KEY=sk-or-...   # or put it in ./.env (see .env.example)

python3 scripts/jev_decide.py --state "What's on my calendar tomorrow?" --preset intent --brief
python3 scripts/jev_mail_triage.py --input examples/mails_sample.json --brief
```

## Tests

Offline unit tests (standard library, no API key, no network) run in CI on every pull request:

```bash
python3 -m unittest discover -s tests -v
```

## Privacy / ZDR

If your OpenRouter account routes `typesafe/jev-*` under zero-data-retention, Jev is fine for the same traffic class as your other ZDR models. Otherwise keep private mail/calendar text out of `state`, or use TypeSafe under their DPA.

## License

MIT
