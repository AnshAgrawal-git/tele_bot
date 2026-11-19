# CLAUDE.md — Play 246 Bot & CRM Project Guide

## Overview
This repository contains a unified **Telegram Bot** and **Web CRM Dashboard** built for Play 246 (gaming/betting lead management and customer support platform).

- **GitHub Repository**: https://github.com/AnshAgrawal-git/tele_bot
- **Live Cloud Deployment**: https://play246-crm-v2.onrender.com (Render free plan, branch `feature/bot-messages-crm`; old service `play246-crm` to be suspended)
- **Health Check / Keep-Alive**: https://play246-crm-v2.onrender.com/health (keep monitored by UptimeRobot every 5m)
- **Bot Username**: `@prototypetelegram247bot`
- **Admin Group**: `Play 246 Admin Inbox` (`-1004349789648`)

> ℹ️ **Render free plan + Supabase Postgres.** Service `play246-crm-v2` (https://play246-crm-v2.onrender.com) runs on Render's free plan; all data lives in Supabase via `DATABASE_URL` (Session pooler, port 5432 — Render has no IPv6). Nothing is lost on deploys/restarts/sleep. Keep `/health` pinged by UptimeRobot every 5 min. Only one copy may poll Telegram: a Postgres advisory lock (`pg_try_advisory_lock(246246)`) makes any extra copy serve the CRM only until the lock frees.

---

## Architecture & Technology Stack
- **Language**: Python 3.10+
- **Telegram Bot Framework**: `aiogram` (v3.31.0) with Forum Topics + FSM (admin menu wizards)
- **Web Server / REST API**: `aiohttp` (v3.11+), embedded directly inside `bot.py`
- **Database**: Supabase Postgres via `asyncpg` when `DATABASE_URL` is set; otherwise local SQLite (`leads.db`) via `aiosqlite`. All SQL is written SQLite-style (`?`) and translated for Postgres in `db_all`/`db_one`/`db_run`/`db_ddl` — always use these helpers.
- **Frontend**: Single-page vanilla HTML/CSS/JS in `crm.html` (no CDN dependencies)
- **Deployment Platform**: Render (`render.yaml`, `Procfile`, `requirements.txt`)

---

## Directory Structure
```
tele_bot/
├── bot.py             # Telegram bot + reply engine + admin menu + aiohttp CRM API
├── crm.html           # CRM frontend (Chats, Bot messages, Payments, Broadcast, Overview)
├── leads.db           # SQLite database (git-ignored)
├── requirements.txt
├── Procfile
├── render.yaml
├── runtime.txt
├── get_group_id.py    # Utility to inspect Telegram chat/topic IDs
└── .env.example
```

---

## Environment Variables (set in Render → Environment)
| Key | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token (rotate in @BotFather; never commit it) |
| `ADMIN_GROUP_ID` | `-1004349789648` |
| `PORT` | `8080` locally, `10000` on Render |
| `CRM_PASSWORD` | Password for the CRM login. Empty = CRM is open (warning shown) |
| `DATABASE_URL` | Supabase Postgres connection string (Session pooler). Empty = local SQLite |
| `DB_PATH` | SQLite file path, only used without `DATABASE_URL` |
| `RUN_BOT` | `0` = web CRM only (no Telegram polling). Keep `0` in your local `.env` |

> When running locally with polling, suspend Render first or you'll get `TelegramConflictError`. Use `RUN_BOT=0` to test only the CRM.

---

## How bot replies work
All replies live in the **`bot_messages`** table. Each message has a `kind` ("when should the bot send this?"):

| kind | When | Notes |
|---|---|---|
| `start` | Customer sends /start | Core (one, can't delete). Always shows "📱 Share Mobile Number" |
| `contact` | Customer shares their number | Core. Topic is created + contact pinned |
| `photo` | Customer sends a photo | Core. Screenshot saved to `media_vault` |
| `fallback` | No keyword matched | Core. Also alerts the team in the topic (with saved-reply buttons) |
| `keywords` | Message contains one of the words | Longest matching word wins. Words ≥4 letters also match word starts ("withdraw" → "withdrawal") |
| `quiet` | Customer silent for N minutes | Background check every 60s, sent once per silence, only within a 2h window |
| `saved` | Never automatic | Team sends via CRM chips or `/r` in a Telegram topic |

**One decision function, `decide_reply()`,** is used by the live bot, the CRM "Try it" box (`/api/simulate`) and the Telegram "🧪 Test the bot" button, so they always agree.

Hidden defaults (`bot_settings` table, editable in CRM → Bot messages → Bot behaviour):
- `handoff_minutes` (10): after a team member replies, keyword/fallback auto-replies pause.
- `alert_team` (1): post an alert in the topic when the bot can't answer.
- Same auto-reply isn't repeated to a user within 1 min (fallback: 30 min).

Placeholders: `{name}`, `{phone}` (stored). The CRM/Telegram show them as `[name]`, `[phone]`, and both forms are rendered.

On first start, defaults are seeded (texts from the legacy `flow_steps` table are reused if present).

---

## Database Schema (Supabase Postgres / `leads.db`)
Move a local `leads.db` into Supabase with `python tools/migrate_to_supabase.py` (keeps ids; `--force` to overwrite).

1. **`users`**: `user_id` (PK), `first_name`, `username`, `phone_number`, `topic_id`, `is_pending`, `last_message`, `last_time`, `last_customer_at`, `last_agent_at`, `created_at` (ISO UTC)
2. **`messages`**: `id`, `user_id`, `sender` ('customer'|'agent'|'bot'), `text`, `media_type`, `timestamp`, `ts` (ISO UTC), `rule_name`
3. **`bot_messages`**: `id`, `name`, `kind`, `keywords`, `match_mode`, `quiet_minutes`, `text`, `buttons` (JSON), `media_id`, `is_active`, `is_core`, `sort_order`, `use_count`, `last_used_at`, `created_at`, `updated_at`
4. **`media`**: `id`, `kind` (photo|video|animation|sticker), `file_id`, `data` (BLOB), `mime`, `filename`
5. **`media_vault`**: `id`, `user_id`, `sender_name`, `phone_number`, `file_id`, `utr_code` (status), `timestamp`, `ts`
6. **`bot_settings`**: `key`, `value`
7. **`auto_log`**: `id`, `user_id`, `message_id`, `kind`, `sent_at`
8. **`flow_steps`**: legacy, only read once for migration

---

## Telegram admin (in the admin group)
- `/menu` — button menu: See bot flow · Edit a message · Add auto-reply · Saved replies · Test the bot · Stats
- `/r` inside a customer topic — tap a saved reply to send it
- `/broadcast`, `/stats`, `/export_leads`, `/cancel`, `/help`
- Legacy commands still work: `/view_templates`, `/set_welcome`, `/set_links`, `/add_step`, `/edit_step`, `/delete_step`, `/set_trigger`

Wizard handlers are registered **before** the topic relay so admin input is never forwarded to customers.

---

## CRM REST API (all `/api/*` need login when `CRM_PASSWORD` is set)
`/api/me`, `/api/login`, `/api/logout`, `/api/stats`, `/api/chats`, `/api/chats/{id}/messages|read|reply|send-saved`,
`/api/bot-messages` (GET/POST), `/api/bot-messages/{id}` (PUT/DELETE), `/api/bot-messages/restore`, `/api/bot-messages/{id}/media` (POST/DELETE),
`/api/media/{id}`, `/api/simulate`, `/api/settings` (GET/PUT), `/api/vault`, `/api/vault/{id}` (PUT), `/api/vault/{id}/image`,
`/api/audience-counts`, `/api/broadcast`, `/api/export/leads.csv`, `/health`

---

## Common Development Commands
```powershell
cd C:\Users\lenovo\Projects\tele_bot
pip install -r requirements.txt

# CRM only (safe while Render is running)
$env:RUN_BOT="0"; python bot.py

# Full bot + CRM (suspend Render first)
python bot.py

# Deploy (Render auto-deploys on push)
git add .
git commit -m "Your commit message"
git push origin main
```

---

## Design & Behavioral Guidelines
1. **Design Theme**: Clean, matte, professional SaaS. No neon or flashy gradients. Light + dark mode via CSS variables in `crm.html`. Responsive (sidebar on desktop, bottom nav on mobile).
2. **Admin is non-technical**: no technical words in the UI ("trigger", "rule", "priority"). Use plain sentences, buttons, previews, undo and confirmations.
3. **Two-Way Sync**: Telegram admin menu and CRM both read/write `bot_messages` — changes apply instantly.
4. **Language**: English for development/admin UI. Customer-facing bot messages are in Hinglish (Roman script Hindi).
