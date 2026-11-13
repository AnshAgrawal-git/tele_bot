# CLAUDE.md — Play 246 Bot & CRM Project Guide

## Overview
This repository contains a unified **Telegram Bot** and **Web CRM Dashboard** built for Play 246 (gaming/betting lead management and customer support platform).

- **GitHub Repository**: https://github.com/AnshAgrawal-git/tele_bot
- **Live Cloud Deployment**: https://play246-crm.onrender.com (Render Free Tier)
- **Health Check / Keep-Alive**: https://play246-crm.onrender.com/health (monitored by UptimeRobot every 5m)
- **Bot Username**: `@prototypetelegram247bot`
- **Admin Group**: `Play 246 Admin Inbox` (`-1004349789648`)

---

## Architecture & Technology Stack
- **Language**: Python 3.10+
- **Telegram Bot Framework**: `aiogram` (v3.31.0) with Forum Topic / Forum Thread support
- **Web Server / REST API**: `aiohttp` (v3.11+), embedded directly inside `bot.py`
- **Database**: SQLite (`leads.db`) managed asynchronously via `aiosqlite`
- **Frontend**: Single-page vanilla HTML5 + Tailwind CSS (via CDN) + Lucide Icons in `crm.html`
- **Deployment Platform**: Render (`render.yaml`, `Procfile`, `requirements.txt`)

---

## Directory Structure
```
telegram-bot/
├── bot.py             # Core entrypoint: Telegram bot handlers + aiohttp web server & REST API
├── crm.html           # Full CRM Frontend dashboard (Chat Inquiries, Flow Studio, Vault, Broadcast)
├── leads.db           # SQLite database storing users, messages, flow_steps, media_vault
├── requirements.txt   # Python dependencies
├── Procfile           # Process runner definition for Render/Heroku
├── render.yaml        # Render cloud infrastructure blueprint
├── runtime.txt        # Python runtime version (python-3.10.7)
├── get_group_id.py    # Utility script to inspect Telegram chat/topic IDs
└── .env.example       # Example configuration variables
```

---

## Key Credentials & Environment Variables
Defined in `bot.py` with fallback defaults or read via environment variables:
- `BOT_TOKEN`: `9011031953:AAEBkNzzKEwc8gDeYSyidt0ff97cFo9aEkc`
- `ADMIN_GROUP_ID`: `-1004349789648`
- `PORT`: Web server port (`8080` locally, `10000` on Render)

> **Important**: When running locally, do not poll the bot if Render is running, otherwise `TelegramConflictError` will occur. If testing bot locally, suspend the Render service temporarily, or test web endpoints only.

---

## Database Schema (`leads.db`)
1. **`users`**: `user_id` (PK), `first_name`, `username`, `phone_number`, `topic_id`, `is_pending`, `last_message`, `last_time`
2. **`messages`**: `id` (PK AUTO), `user_id`, `sender` ('customer'|'agent'|'bot'), `text`, `media_type`, `timestamp`
3. **`flow_steps`**: `id` (PK AUTO), `step_order`, `title`, `trigger_type`, `content`, `is_active`
4. **`media_vault`**: `id` (PK AUTO), `user_id`, `sender_name`, `phone_number`, `file_id`, `utr_code`, `timestamp`

---

## Common Development Commands

### Local Execution (PowerShell):
```powershell
# Navigate to directory
cd C:\Users\lenovo\.gemini\antigravity\scratch\telegram-bot

# Run server + bot locally
python bot.py
```

### Git & Deployment:
```powershell
# Check git status
git status

# Commit and push changes to GitHub (Render auto-deploys on push)
git add .
git commit -m "Your commit message"
git push origin main
```

---

## Design & Behavioral Guidelines
1. **Design Theme**: Clean, matte, professional SaaS aesthetic. Avoid loud neon colors or flashy gradients. Support both Light and Dark mode using CSS custom variables in `crm.html`.
2. **Two-Way Synchronization**: Edits to bot templates/flow steps in Telegram commands (`/set_welcome`, `/set_links`, `/edit_step`) and in the CRM web UI both update the `flow_steps` SQLite table in real-time.
3. **Customer Language / Persona**: English language communication for development. Customer-facing bot strings are in Hinglish (Roman script Hindi) as requested by the client Play 246.
