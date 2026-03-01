# Aljamri CrewAI Orchestrator

CrewAI-based orchestration for the Aljamri group Telegram agents:

- `aljamrigroupbot` (master coordinator)
- `paylobot` (Paylo specialist)
- `skyhubtravelbot` (Skyhub specialist)

The master routes each incoming group message to Paylo, Skyhub, or both; workers return domain-specific replies; master returns shared improvement notes.

## Why this module

- Uses the **CrewAI** multi-agent framework directly.
- Enforces your hierarchy: workers follow master coordination.
- Produces a single JSON output for external dispatchers (OpenClaw, API workers, cron jobs, etc.).

## Setup

```bash
cd /Users/mohammadalameemi/Desktop/ALJAMRI\ GROUP/agent-project/crewai-orchestrator
uv venv --python 3.11
source .venv/bin/activate
uv sync
```

Copy environment template and fill credentials:

```bash
cp .env.example .env
```

At minimum, set `OPENAI_API_KEY` (or use an OpenAI-compatible endpoint with `OPENAI_BASE_URL`).

## Run

Dry run (no LLM call):

```bash
python src/aljamri_crew/main.py --message "Need transfer rate and hotel options" --sender "@mohammad" --chat-id "-1003565356581" --dry-run --pretty
```

Live run (CrewAI + LLM):

```bash
python src/aljamri_crew/main.py --message "Need transfer rate and hotel options" --sender "@mohammad" --chat-id "-1003565356581" --pretty
```

## Telegram bridge (production use)

Run the long-polling dispatcher that:

1. Reads group updates from `aljamrigroupbot`.
2. Runs CrewAI orchestration per message.
3. Sends replies as `aljamrigroupbot`, `paylobot`, and `skyhubtravelbot`.
4. Preserves all messages (no delete/suppress behavior).

```bash
python src/aljamri_crew/telegram_bridge.py
```

Useful flags:

```bash
# one poll cycle only (smoke test)
python src/aljamri_crew/telegram_bridge.py --once

# force deterministic mode (no LLM calls)
python src/aljamri_crew/telegram_bridge.py --dry-run
```

Required env vars for bridge:

- `ALJAMRI_MASTER_BOT_TOKEN`
- `ALJAMRI_PAYLO_BOT_TOKEN`
- `ALJAMRI_SKYHUB_BOT_TOKEN`

Recommended Telegram BotFather settings for all three bots:

- Disable privacy mode (`/setprivacy` -> `Disable`) so group messages are visible.
- Keep bots as admins if you want full message visibility in restricted groups.

## Exa web search (optional, capped)

You can ground responses with Exa web results when messages ask for recent/current info.

1. Set `EXA_API_KEY` in `.env`.
2. Keep `ALJAMRI_EXA_ENABLED=true`.
3. Hardcap is enforced locally with `ALJAMRI_EXA_HARDCAP_REQUESTS` (default `1000`).

Usage is tracked per UTC month in `ALJAMRI_EXA_USAGE_FILE` (default `.runtime/exa_usage.json`).
When the cap is hit, orchestration continues without Exa calls.

## Output contract

The CLI returns JSON:

```json
{
  "master_bot": "aljamrigroupbot",
  "master_reply": "string",
  "replies": [
    { "bot": "paylobot", "text": "string" },
    { "bot": "skyhubtravelbot", "text": "string" }
  ],
  "routing": { "paylo": true, "skyhub": true },
  "cross_improvement_notes": ["string"],
  "raw": "raw crew output"
}
```

## Dispatcher integration example

Your Telegram dispatcher can execute:

```bash
python src/aljamri_crew/main.py --message "$MESSAGE_TEXT" --sender "$SENDER" --chat-id "$CHAT_ID"
```

Then:

1. Send `master_reply` from `aljamrigroupbot`.
2. Send each item in `replies` from its corresponding bot identity.
3. Keep message history intact (no deletion) to preserve cross-agent learning context.
