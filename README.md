# Telegram Bot — Lamix SMS Reseller Panel Integration

A production Telegram bot that gives an SMS-reselling agent's clients self-service access to their Lamix panel account — requesting numbers, checking their own CDR history, and receiving a live feed of incoming traffic — without ever touching the Lamix web panel directly.

Built and deployed for a live business (client accounts, real inventory, real money), currently running 24/7 on a Linux VPS.

## What it does

**For clients (linked to a Lamix account):**
- `/link_acc` / `/unlink_acc` — self-service account linking, validated against the agent's real client roster, auto-approved on a valid match
- `/addnum` — browse live number-range inventory (grouped, paginated from the Lamix API), request a quantity, and get real MSISDNs assigned to their account on the spot
- `/my_cdr` — pull their own message/CDR history for the last 10 minutes, filtered out of the agent's full traffic firehose
- A rolling 24-hour, per-client, per-range quota (enforced server-side, not just client-side) to prevent inventory abuse

**For the agent (admin):**
- `/adduser`, `/removeuser` — allow-list management, independent of Lamix account linking
- `/syncclients` — pull the live client roster from Lamix instead of registering usernames by hand
- A live channel feed: every incoming SMS gets posted to a Telegram channel in near-real-time, with the phone number partially masked and one-tap "copy" buttons for the CLI and extracted verification code

## Why it's interesting technically

- **Rate-limit-aware pagination against a real third-party REST API.** Lamix caps responses at 500 records/call with no cursor on some endpoints — the CDR lookup recursively bisects a time window only when a page comes back full, instead of blindly paginating a potentially enormous dataset (one client alone held 13,000+ assigned numbers in production).
- **A live "what's new" feed built without a webhook.** No `client` filter exists on Lamix's message endpoint, so the channel feed polls on a cursor persisted in SQLite (survives restarts, no gaps, no dupes) rather than an in-memory "last seen" timestamp.
- **Self-healing background jobs.** A held-numbers cache is pre-warmed on a schedule so an interactive command never blocks on a slow upstream pagination call the user is actually waiting on.
- **Deployed as a real systemd service** on a Linux VPS with `Restart=always`, after diagnosing and fixing a nasty Windows-specific bug during earlier local hosting (a supervisor script's `-Wait` silently failing to block, leading to multiple zombie instances of the bot polling the same Telegram token simultaneously — root-caused via process inspection, not guesswork).

## Tech stack

- Python 3.10+, [`python-telegram-bot`](https://github.com/python-telegram-bot/python-telegram-bot) (async, `JobQueue`, `PicklePersistence`)
- `httpx` for the Lamix REST API client
- SQLite for local state (members, allow-list, client roster, quota history, number-cache)
- systemd for process supervision in production

## Project structure

```
bot.py          Telegram handlers, commands, jobs
lamix_api.py    Thin async client for the Lamix panel REST API
db.py           SQLite access layer
config.py       Environment-based configuration
requirements.txt
```

## Running it yourself

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your own bot token, Lamix API token, etc.
python bot.py
```

See `.env.example` for every configuration option (bot token, admin IDs, required channels, Lamix API credentials, traffic channel ID).

---

*Built by M Mohsin Ali.*
