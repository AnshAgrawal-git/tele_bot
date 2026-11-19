"""
Play 246 — Telegram Support Bot + Web CRM
=========================================
One process runs:
  • the Telegram bot (aiogram 3, long polling)
  • the CRM web server + REST API (aiohttp)
Both share one SQLite database, so edits made in Telegram show up in the CRM
instantly and vice-versa.

Bot replies are driven by the `bot_messages` table. Every incoming customer
event goes through ONE decision function (`decide_reply`) so the CRM
"Try it" box, the Telegram "Test the bot" button and the real bot can never
disagree.
"""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==============================================================================
# CONFIGURATION  (set these in Render → Environment)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Read KEY=VALUE lines from a local .env (Render uses real env vars instead)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(BASE_DIR, ".env"))

# TODO: rotate this token in @BotFather and remove the fallback once
# BOT_TOKEN is confirmed in Render's environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", "9011031953:AAEBkNzzKEwc8gDeYSyidt0ff97cFo9aEkc")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1004349789648"))
WEB_PORT = int(os.getenv("PORT", 8080))
# Supabase / Postgres connection string. When set, ALL data lives in Postgres and
# survives deploys/restarts. When empty, the local SQLite file (DB_PATH) is used.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
# Only used without DATABASE_URL (local testing).
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "leads.db"))
CRM_HTML_PATH = os.path.join(BASE_DIR, "crm.html")
CRM_URL = os.getenv("CRM_URL", "https://play246-crm.onrender.com")
# Password for the web CRM. If empty, the CRM is open (a warning is shown).
CRM_PASSWORD = os.getenv("CRM_PASSWORD", "")
# Set RUN_BOT=0 to run only the web CRM locally (avoids TelegramConflictError).
RUN_BOT = os.getenv("RUN_BOT", "1") != "0"

IST = timezone(timedelta(hours=5, minutes=30))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Kinds of bot messages ("when should the bot send this?")
CORE_KINDS = ("start", "contact", "photo", "fallback")   # exactly one each, can't be deleted
EXTRA_KINDS = ("keywords", "quiet", "saved", "after")    # admin can add many
ALL_KINDS = CORE_KINDS + EXTRA_KINDS

KIND_INFO = {
    "start":    ("👋", "When a customer starts the bot"),
    "contact":  ("📱", "When a customer shares their number"),
    "photo":    ("🧾", "When a customer sends a photo"),
    "keywords": ("💬", "When a customer's message mentions certain words"),
    "quiet":    ("⏰", "When a customer goes quiet"),
    "fallback": ("🤷", "When the bot doesn't understand"),
    "saved":    ("📌", "Only when your team sends it"),
    "after":    ("↪️", "After another message is sent"),
}

DEFAULT_SETTINGS = {
    # Bot stays quiet (no auto answers) for N minutes after a team member replies.
    "handoff_minutes": "10",
    # Post an alert in the customer's topic when the bot can't answer.
    "alert_team": "1",
}


# ==============================================================================
# SMALL HELPERS
# ==============================================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def ist_clock() -> str:
    return datetime.now(IST).strftime("%I:%M %p")


def ist_midnight_utc_iso() -> str:
    midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def fmt_minutes(n: int) -> str:
    n = int(n or 0)
    if n < 60:
        return f"{n} minute" + ("" if n == 1 else "s")
    if n % 1440 == 0:
        d = n // 1440
        return f"{d} day" + ("" if d == 1 else "s")
    if n % 60 == 0:
        h = n // 60
        return f"{h} hour" + ("" if h == 1 else "s")
    return f"{n // 60} h {n % 60} min"


def split_keywords(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[,\n]", raw or "")
    seen, out = set(), []
    for k in items:
        k = str(k).strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def normalize(text: str) -> str:
    """Lower-case, strip punctuation/emoji, collapse spaces. Keeps Hindi matras."""
    text = (text or "").lower()
    chars = []
    for ch in text:
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            chars.append(ch)
        else:
            chars.append(" ")
    return " ".join("".join(chars).split())


def keyword_hits(norm_text: str, keyword: str, mode: str = "contains") -> bool:
    nk = normalize(keyword)
    if not nk:
        return False
    if mode == "exact":
        return norm_text == nk
    padded = f" {norm_text} "
    if f" {nk} " in padded:
        return True
    # Longer keywords may also match the start of a word: "withdraw" ~ "withdrawal"
    return len(nk) >= 4 and f" {nk}" in padded


def render_text(text: str, name: str = "Player", phone: str = "") -> str:
    text = text or ""
    text = re.sub(r"[\{\[]\s*name\s*[\}\]]", name or "Player", text, flags=re.I)
    text = re.sub(r"[\{\[]\s*phone\s*[\}\]]", phone or "", text, flags=re.I)
    return text


def delay_label(n) -> str:
    n = int(n or 0)
    return "Right away" if n <= 0 else f"{fmt_minutes(n)} later"


def when_sentence(m: dict, parent_name: str = None) -> str:
    kind = m.get("kind")
    if kind == "after":
        n = int(m.get("delay_minutes") or 0)
        target = f"“{parent_name}”" if parent_name else "the message above"
        return (f"Right after {target} is sent" if n <= 0
                else f"{fmt_minutes(n)} after {target} is sent")
    if kind == "keywords":
        kws = split_keywords(m.get("keywords"))
        if not kws:
            return "When a customer says… (no words set yet)"
        shown = ", ".join(f"“{k}”" for k in kws[:3])
        more = f" +{len(kws) - 3} more" if len(kws) > 3 else ""
        return f"When a customer says {shown}{more}"
    if kind == "quiet":
        return f"When a customer is quiet for {fmt_minutes(m.get('quiet_minutes') or 15)}"
    return KIND_INFO.get(kind, ("", kind))[1]


# ==============================================================================
# DATABASE
# ==============================================================================
# All SQL is written once in SQLite style ("?" placeholders). With DATABASE_URL set,
# it is translated for Postgres (asyncpg) on the fly, so both backends share one code path.
_ID_TABLES = {"messages", "flow_steps", "media_vault", "bot_messages", "media", "auto_log", "scheduled_sends"}
_pg_pool = None
_pg_pool_lock = asyncio.Lock()


def _pg_sql(sql: str) -> str:
    """Turn ? placeholders into $1, $2… (ignoring any ? inside quoted strings)."""
    out, n, in_str = [], 0, False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
        if ch == "?" and not in_str:
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


def _pg_ddl(sql: str) -> str:
    return (sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
               .replace("BLOB", "BYTEA").replace("INTEGER", "BIGINT"))


async def _pg():
    global _pg_pool
    if _pg_pool is None:
        async with _pg_pool_lock:
            if _pg_pool is None:
                import asyncpg
                # statement_cache_size=0 keeps it compatible with Supabase's poolers.
                _pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5,
                                                     statement_cache_size=0, command_timeout=30)
    return _pg_pool


async def db_all(sql: str, args=()) -> list:
    if USE_PG:
        rows = await (await _pg()).fetch(_pg_sql(sql), *args)
        return [dict(r) for r in rows]
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_one(sql: str, args=()):
    rows = await db_all(sql, args)
    return rows[0] if rows else None


async def db_run(sql: str, args=()) -> int:
    """Run INSERT/UPDATE/DELETE. For INSERTs into tables with an id column, returns the new id."""
    if USE_PG:
        pool = await _pg()
        m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
        if m and m.group(1).lower() in _ID_TABLES and "RETURNING" not in sql.upper():
            return await pool.fetchval(_pg_sql(sql) + " RETURNING id", *args)
        await pool.execute(_pg_sql(sql), *args)
        return 0
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


async def db_ddl(sql: str):
    if USE_PG:
        await (await _pg()).execute(_pg_ddl(sql))
    else:
        await db_run(sql)


async def _add_columns(table: str, cols: list):
    if USE_PG:
        for name, definition in cols:
            await db_ddl(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}")
        return
    existing = {r["name"] for r in await db_all(f"PRAGMA table_info({table})")}
    for name, definition in cols:
        if name not in existing:
            await db_run(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


DEFAULT_WELCOME = (
    "👋 Welcome {name} to Play 246 Auto ID!\n\n"
    "⬇️ OFFER LENE KE LIYE ⬇️\n"
    "👉 Neeche 📱 SHARE MOBILE NUMBER button dabayein.\n"
    "❌ Number type na karein."
)
DEFAULT_LINKS = (
    "✅ Aapka Offer Activate Ho Gaya Hai!\n\n"
    "🟠 SITE 1 – (Auto Site): https://play246.co/?code=Play246\n"
    "🟢 SITE 2 – (Lotus/Ultra): https://play246.fun/home\n"
    "🔵 SITE 3 – (Diamond): https://portal246.com/home\n\n"
    "Min Deposit : ₹500 | Demo ID Available"
)
DEFAULT_FOLLOWUP = (
    "Need help creating your Demo ID? Reply here or tap our VIP WhatsApp agent: "
    "https://walink.online/Play246"
)
WHATSAPP_BTN = [{"label": "💬 WhatsApp Agent", "url": "https://walink.online/Play246"}]


def default_messages(legacy: dict) -> list:
    """Ready-made messages. `legacy` carries texts from the old flow_steps table."""
    return [
        # --- main flow (core) ---
        dict(kind="start", name="Welcome message", text=legacy.get("start") or DEFAULT_WELCOME, sort_order=1),
        dict(kind="contact", name="Site links", text=legacy.get("contact") or DEFAULT_LINKS, sort_order=2),
        dict(kind="photo", name="Screenshot received",
             text="✅ Screenshot mil gaya {name} ji!\n\nHamari team 5-10 minute mein verify karke aapko update karegi. Thoda wait karein 🙏",
             sort_order=3),
        dict(kind="fallback", name="Didn't understand",
             text="🙏 Aapka message mil gaya hai!\n\nHamari team jaldi hi aapko reply karegi.\n\nTab tak aap yeh likh sakte hain:\n• deposit\n• withdrawal\n• demo id",
             sort_order=4),
        # --- ready-made answers (keywords) ---
        dict(kind="keywords", name="Deposit help",
             keywords="deposit, jama, paise dalne, paisa dalna, add money, recharge, payment kaise, upi",
             text="💰 Deposit karna bahut aasaan hai!\n\n1️⃣ Site par login karein\n2️⃣ Deposit / Add Money par click karein\n3️⃣ Payment karke screenshot yahan bhejein\n\nMin Deposit: ₹500",
             sort_order=10),
        dict(kind="keywords", name="Withdrawal help",
             keywords="withdraw, withdrawal, nikalna, paise nikalne, payout, redeem",
             text="💸 Withdrawal ke liye apna registered number aur amount yahan bhejein.\n\nHamari team jaldi process karegi.",
             sort_order=11),
        dict(kind="keywords", name="Demo ID",
             keywords="demo, demo id, new id, id chahiye, id banana, account kaise, register",
             text="🎮 Demo ID ke liye bas apna naam aur number yahan bhejein — hamari team turant bana degi!",
             sort_order=12),
        dict(kind="keywords", name="Site links (on request)",
             keywords="link, site, website, url",
             text=legacy.get("contact") or DEFAULT_LINKS, sort_order=13),
        dict(kind="keywords", name="Bonus & offers",
             keywords="bonus, offer, cashback, promo",
             text="🎁 Bonus aur offers ki jaankari ke liye hamari team aapko abhi reply karegi. Apna registered number bhej dein.",
             sort_order=14),
        dict(kind="keywords", name="Talk to an agent",
             keywords="agent, call, whatsapp, support, baat karni, help",
             text="👨‍💼 Hamari team yahin aapko reply karegi.\n\nJaldi baat karni ho to WhatsApp par message karein 👇",
             buttons=WHATSAPP_BTN, sort_order=15),
        # --- follow-up ---
        dict(kind="quiet", name="Follow-up reminder", quiet_minutes=legacy.get("quiet_minutes") or 15,
             text=legacy.get("quiet") or DEFAULT_FOLLOWUP, sort_order=20),
        # --- saved replies for the team ---
        dict(kind="saved", name="Ask for UTR screenshot",
             text="🙏 Please apna transaction UTR screenshot yahan bhejein.", sort_order=30),
        dict(kind="saved", name="Demo ID ready",
             text="✅ Aapki Demo ID ban gayi hai {name} ji. Enjoy playing! 🎮", sort_order=31),
        dict(kind="saved", name="Payment verified",
             text="✅ Aapka payment verify ho gaya hai. Balance update kar diya gaya hai.", sort_order=32),
        dict(kind="saved", name="Bonus added",
             text="🎉 Aapka bonus successfully add ho gaya hai!", sort_order=33),
    ]


async def init_db():
    if True:
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                phone_number TEXT,
                topic_id INTEGER,
                is_pending INTEGER DEFAULT 0,
                last_message TEXT,
                last_time TEXT
            )""")
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sender TEXT,
                text TEXT,
                media_type TEXT,
                timestamp TEXT
            )""")
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS flow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_order INTEGER,
                title TEXT,
                trigger_type TEXT,
                content TEXT,
                is_active INTEGER DEFAULT 1
            )""")  # legacy — kept only to migrate old texts
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS media_vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sender_name TEXT,
                phone_number TEXT,
                file_id TEXT,
                utr_code TEXT,
                timestamp TEXT
            )""")
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS bot_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                keywords TEXT DEFAULT '',
                match_mode TEXT DEFAULT 'contains',
                quiet_minutes INTEGER DEFAULT 15,
                text TEXT DEFAULT '',
                buttons TEXT DEFAULT '[]',
                media_id INTEGER,
                is_active INTEGER DEFAULT 1,
                is_core INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 100,
                use_count INTEGER DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )""")
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,
                file_id TEXT,
                data BLOB,
                mime TEXT,
                filename TEXT,
                created_at TEXT
            )""")
        await db_ddl("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)")
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS auto_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                kind TEXT,
                sent_at TEXT
            )""")

        await _add_columns("users", [
            ("is_pending", "INTEGER DEFAULT 0"), ("last_message", "TEXT"), ("last_time", "TEXT"),
            ("last_customer_at", "TEXT"), ("last_agent_at", "TEXT"), ("created_at", "TEXT"),
        ])
        await _add_columns("messages", [("rule_name", "TEXT"), ("ts", "TEXT")])
        await _add_columns("media_vault", [("ts", "TEXT")])
        await _add_columns("bot_messages", [
            ("parent_id", "INTEGER"), ("delay_minutes", "INTEGER DEFAULT 0"), ("skip_if_replied", "INTEGER DEFAULT 1"),
        ])
        await db_ddl("""
            CREATE TABLE IF NOT EXISTS scheduled_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                due_at TEXT,
                created_at TEXT
            )""")

        for k, v in DEFAULT_SETTINGS.items():
            await db_run("INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING", (k, v))

        # Seed bot messages on first run (re-uses texts from the old flow_steps table)
        has_messages = (await db_one("SELECT COUNT(*) AS n FROM bot_messages"))["n"] > 0
        if not has_messages:
            legacy = {}
            for row in await db_all("SELECT trigger_type, content FROM flow_steps ORDER BY step_order"):
                    trig, content = row["trigger_type"], row["content"]
                    trig = (trig or "").strip()
                    if trig == "/start" and "start" not in legacy:
                        legacy["start"] = content
                    elif trig == "contact_shared" and "contact" not in legacy:
                        legacy["contact"] = content
                    elif trig.startswith("inactivity") and "quiet" not in legacy:
                        legacy["quiet"] = content
                        m = re.search(r"(\d+)", trig)
                        legacy["quiet_minutes"] = int(m.group(1)) if m else 15
            ts = now_iso()
            for m in default_messages(legacy):
                await db_run(
                    """INSERT INTO bot_messages (name, kind, keywords, quiet_minutes, text, buttons,
                       is_active, is_core, sort_order, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                    (m["name"], m["kind"], m.get("keywords", ""), m.get("quiet_minutes", 15), m["text"],
                     json.dumps(m.get("buttons", []), ensure_ascii=False),
                     1 if m["kind"] in CORE_KINDS else 0, m.get("sort_order", 100), ts, ts),
                )


async def get_setting(key: str) -> str:
    row = await db_one("SELECT value FROM bot_settings WHERE key = ?", (key,))
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


async def set_setting(key: str, value: str):
    await db_run("INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


async def get_message(msg_id: int):
    return await db_one("SELECT * FROM bot_messages WHERE id = ?", (msg_id,))


async def get_core(kind: str):
    return await db_one("SELECT * FROM bot_messages WHERE kind = ? ORDER BY id LIMIT 1", (kind,))


async def list_messages(kind: str = None, active_only: bool = False) -> list:
    sql = "SELECT * FROM bot_messages WHERE 1=1"
    args = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY sort_order, id"
    return await db_all(sql, args)


async def log_message(user_id: int, sender: str, text: str, media_type: str = "text",
                      rule_name: str = None, pending="auto"):
    """Store a chat line and update the customer's summary row.
    pending: "auto" (customer→1, agent→0, bot→unchanged), 0, 1 or "keep"."""
    ts = now_iso()
    clock = ist_clock()
    await db_run(
        "INSERT INTO messages (user_id, sender, text, media_type, timestamp, rule_name, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, sender, text, media_type, clock, rule_name, ts),
    )
    if pending == "auto":
        pending = {"customer": 1, "agent": 0}.get(sender, "keep")
    sets = ["last_message = ?", "last_time = ?"]
    args = [(text or "")[:100], clock]
    if pending != "keep":
        sets.append("is_pending = ?")
        args.append(int(pending))
    if sender == "agent":
        sets.append("last_agent_at = ?")
        args.append(ts)
    args.append(user_id)
    await db_run(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", args)


async def touch_customer(user: types.User):
    """Create/refresh the customer row and mark them as just active."""
    ts = now_iso()
    await db_run(
        """INSERT INTO users (user_id, first_name, username, last_time, last_customer_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               first_name = excluded.first_name,
               username = excluded.username,
               last_customer_at = excluded.last_customer_at,
               created_at = COALESCE(users.created_at, excluded.created_at)""",
        (user.id, user.first_name or "Player", user.username or "", ist_clock(), ts, ts),
    )


async def get_or_create_topic(user: types.User, phone: str = None):
    row = await db_one("SELECT topic_id FROM users WHERE user_id = ?", (user.id,))
    topic_id = row["topic_id"] if row else None
    if topic_id:
        return topic_id

    phone_label = f" (+{phone.lstrip('+')})" if phone else ""
    topic_title = f"{user.first_name}{phone_label}"[:128]
    try:
        topic = await bot.create_forum_topic(chat_id=ADMIN_GROUP_ID, name=topic_title)
        topic_id = topic.message_thread_id
        await db_run(
            """INSERT INTO users (user_id, first_name, username, phone_number, topic_id, is_pending, last_time, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   username = excluded.username,
                   phone_number = COALESCE(excluded.phone_number, users.phone_number),
                   topic_id = excluded.topic_id""",
            (user.id, user.first_name, user.username or "", phone, topic_id, ist_clock(), now_iso()),
        )
    except Exception as e:
        logging.error(f"Failed to create forum topic: {e}")
        return None
    return topic_id


# ==============================================================================
# SENDING BOT MESSAGES (text + optional photo/video/GIF/sticker + link buttons)
# ==============================================================================
def parse_buttons(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw or "[]")
        except (ValueError, TypeError):
            items = []
    out = []
    for b in items:
        label = str(b.get("label", "")).strip()
        url = str(b.get("url", "")).strip()
        if label and re.match(r"^(https?://|tg://)", url, re.I):
            out.append({"label": label[:60], "url": url})
    return out[:6]


def build_buttons(msg: dict):
    buttons = parse_buttons(msg.get("buttons"))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b["label"], url=b["url"])] for b in buttons
    ])


async def get_media_row(media_id):
    if not media_id:
        return None
    return await db_one("SELECT id, kind, file_id, mime, filename, data IS NOT NULL AS has_data "
                        "FROM media WHERE id = ?", (media_id,))


async def _send_media(chat_id, media: dict, caption=None, reply_markup=None, thread_id=None):
    kind = media.get("kind") or "photo"
    file_ref = media.get("file_id")
    if not file_ref:
        row = await db_one("SELECT data, filename FROM media WHERE id = ?", (media["id"],))
        if not row or row["data"] is None:
            return None
        file_ref = BufferedInputFile(row["data"], filename=row["filename"] or f"media.{kind}")
    common = dict(chat_id=chat_id, reply_markup=reply_markup, message_thread_id=thread_id)
    if kind == "sticker":
        sent = await bot.send_sticker(sticker=file_ref, **common)
        new_id = sent.sticker.file_id if sent.sticker else None
    elif kind == "video":
        sent = await bot.send_video(video=file_ref, caption=caption, **common)
        new_id = sent.video.file_id if sent.video else None
    elif kind == "animation":
        sent = await bot.send_animation(animation=file_ref, caption=caption, **common)
        new_id = sent.animation.file_id if sent.animation else None
    else:
        sent = await bot.send_photo(photo=file_ref, caption=caption, **common)
        new_id = sent.photo[-1].file_id if sent.photo else None
    # Cache Telegram's file_id so later sends are instant
    if new_id and not media.get("file_id") and media.get("id"):
        await db_run("UPDATE media SET file_id = ? WHERE id = ?", (new_id, media["id"]))
    return sent


async def deliver(chat_id, msg: dict, name="Player", phone="", reply_markup=None, thread_id=None) -> str:
    """Send a bot message. Returns the text that was sent (for logging)."""
    text = render_text(msg.get("text") or "", name, phone)
    markup = reply_markup if reply_markup is not None else build_buttons(msg)
    media = msg.get("_media") or await get_media_row(msg.get("media_id"))

    if media:
        can_caption = media.get("kind") != "sticker" and len(text) <= 1024
        await _send_media(chat_id, media, caption=text if (can_caption and text) else None,
                          reply_markup=markup if (can_caption or not text) else None, thread_id=thread_id)
        if text and not can_caption:
            await bot.send_message(chat_id, text, reply_markup=markup, message_thread_id=thread_id)
    elif text.strip():
        await bot.send_message(chat_id, text, reply_markup=markup, message_thread_id=thread_id)
    else:
        return ""
    return text


async def record_auto(user_id: int, msg: dict):
    ts = now_iso()
    await db_run("INSERT INTO auto_log (user_id, message_id, kind, sent_at) VALUES (?, ?, ?, ?)",
                 (user_id, msg["id"], msg["kind"], ts))
    await db_run("UPDATE bot_messages SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                 (ts, msg["id"]))


async def children_of(msg_id: int, active_only: bool = True) -> list:
    sql = "SELECT * FROM bot_messages WHERE kind = 'after' AND parent_id = ?"
    if active_only:
        sql += " AND is_active = 1"
    return await db_all(sql + " ORDER BY delay_minutes, sort_order, id", (msg_id,))


async def descendants_of(msg_id: int) -> list:
    out, queue = [], [msg_id]
    while queue and len(out) < 200:
        kids = await children_of(queue.pop(0), active_only=False)
        out += kids
        queue += [k["id"] for k in kids]
    return out


async def after_sent(user_id: int, msg: dict, depth: int = 0):
    """A bot message was just sent to a customer: queue (or send) the messages that come after it."""
    if depth > 10:
        return
    for child in await children_of(msg["id"]):
        delay = int(child.get("delay_minutes") or 0)
        if delay <= 0:
            await send_scheduled(user_id, child, depth=depth + 1)
        else:
            due = (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat(timespec="seconds")
            await db_run("INSERT INTO scheduled_sends (user_id, message_id, due_at, created_at) VALUES (?, ?, ?, ?)",
                         (user_id, child["id"], due, now_iso()))


async def send_scheduled(user_id: int, msg: dict, depth: int = 0):
    u = await db_one("SELECT first_name, phone_number, topic_id FROM users WHERE user_id = ?", (user_id,))
    if not u:
        return
    sent = await deliver(user_id, msg, name=u["first_name"], phone=u["phone_number"] or "")
    await record_auto(user_id, msg)
    await log_message(user_id, "bot", sent, rule_name=msg["name"], pending="keep")
    if int(msg.get("delay_minutes") or 0) > 0:
        await topic_note(u["topic_id"], f"↪️ Scheduled message sent · {msg['name']}")
    await after_sent(user_id, msg, depth)


async def run_scheduled_sends():
    due = await db_all("SELECT * FROM scheduled_sends WHERE due_at <= ? ORDER BY due_at LIMIT 100", (now_iso(),))
    for row in due:
        await db_run("DELETE FROM scheduled_sends WHERE id = ?", (row["id"],))
        msg = await get_message(row["message_id"])
        if not msg or not msg["is_active"]:
            continue
        if msg.get("skip_if_replied", 1):
            u = await db_one("SELECT last_customer_at, last_agent_at FROM users WHERE user_id = ?", (row["user_id"],))
            if u and any((x or "") > row["created_at"] for x in (u["last_customer_at"], u["last_agent_at"])):
                continue  # the conversation moved on — don't interrupt it
        try:
            await send_scheduled(row["user_id"], msg)
        except Exception as e:
            logging.warning(f"Scheduled send to {row['user_id']} failed: {e}")


async def topic_note(topic_id, text: str, markup=None):
    if not topic_id:
        return
    try:
        await bot.send_message(ADMIN_GROUP_ID, text, message_thread_id=topic_id, reply_markup=markup)
    except Exception as e:
        logging.warning(f"Could not post note in topic {topic_id}: {e}")


async def saved_reply_keyboard(customer_id: int):
    saved = await list_messages("saved", active_only=True)
    if not saved:
        return None
    rows, row = [], []
    for m in saved[:10]:
        row.append(InlineKeyboardButton(text=f"📌 {m['name'][:28]}", callback_data=f"sr:{m['id']}:{customer_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==============================================================================
# THE DECISION ENGINE — used by the bot, the CRM "Try it" box and Telegram test
# ==============================================================================
async def decide_reply(event: str, text: str = "", user_id: int = None, simulate: bool = False) -> dict:
    """
    event: start | contact | photo | text
    Returns {"message": row|None, "reason": plain-English str, "matched": word|None,
             "send": bool, "alert_team": bool}
    """
    result = {"message": None, "reason": "", "matched": None, "send": False, "alert_team": False}

    if event in ("start", "contact", "photo"):
        msg = await get_core(event)
        label = {"start": "starts the bot", "contact": "shares their number", "photo": "sends a photo"}[event]
        if not msg:
            result["reason"] = f"No message is set for when a customer {label}."
        elif not msg["is_active"]:
            result["reason"] = f"The “{msg['name']}” message is switched off, so the bot stays silent."
        else:
            result.update(message=msg, send=True, reason=f"The customer {label}, so the bot sends “{msg['name']}”.")
        return result

    # --- customer typed something ---
    norm = normalize(text)
    best, best_word, best_len = None, None, -1
    for m in await list_messages("keywords", active_only=True):
        for kw in split_keywords(m["keywords"]):
            if keyword_hits(norm, kw, m.get("match_mode") or "contains"):
                n = len(normalize(kw))
                if n > best_len:   # the most specific (longest) word wins; ties → list order
                    best, best_word, best_len = m, kw, n

    if best:
        result.update(message=best, matched=best_word, send=True,
                      reason=f"The message mentions “{best_word}”, so the bot sends “{best['name']}”.")
    else:
        fb = await get_core("fallback")
        if fb and fb["is_active"]:
            result.update(message=fb, send=True,
                          reason=f"No words matched, so the bot sends “{fb['name']}” and alerts your team.")
        else:
            result["reason"] = "No words matched and the “didn't understand” message is off, so the bot stays silent. Your team still sees the message."
        result["alert_team"] = (await get_setting("alert_team")) == "1"

    # A team member is already talking to this customer → bot stays quiet
    if user_id:
        u = await db_one("SELECT last_agent_at FROM users WHERE user_id = ?", (user_id,))
        handoff = int(await get_setting("handoff_minutes") or 0)
        last_agent = parse_iso(u["last_agent_at"]) if u else None
        if handoff and last_agent and datetime.now(timezone.utc) - last_agent < timedelta(minutes=handoff):
            result.update(send=False, alert_team=False,
                          reason=f"Your team replied to this customer in the last {fmt_minutes(handoff)}, so the bot stays quiet and lets them talk.")
            return result

        # Don't repeat the same auto answer too often
        if not simulate and result["message"]:
            window = 30 if result["message"]["kind"] == "fallback" else 1
            since = (datetime.now(timezone.utc) - timedelta(minutes=window)).isoformat(timespec="seconds")
            recent = await db_one("SELECT 1 AS x FROM auto_log WHERE user_id = ? AND message_id = ? AND sent_at >= ?",
                                  (user_id, result["message"]["id"], since))
            if recent:
                result.update(send=False, reason="Sent very recently — not repeating it.")
    return result


async def chain_preview(msg_id: int, total: int = 0, depth: int = 0) -> list:
    """Flattened list of messages that would follow msg_id, with cumulative timing."""
    out = []
    if depth > 10:
        return out
    for c in await children_of(msg_id):
        t = total + int(c.get("delay_minutes") or 0)
        out.append({"msg": c, "minutes": t, "when": "Right away" if t <= 0 else f"{fmt_minutes(t)} later"})
        out += await chain_preview(c["id"], t, depth + 1)
    return out


# ==============================================================================
# TELEGRAM — CUSTOMER SIDE (private chat)
# ==============================================================================
def contact_keyboard():
    btn = KeyboardButton(text="📱 Share Mobile Number", request_contact=True)
    return ReplyKeyboardMarkup(keyboard=[[btn]], resize_keyboard=True, one_time_keyboard=True)


@dp.message(CommandStart(), F.chat.type == "private")
async def start_handler(message: types.Message):
    user = message.from_user
    await touch_customer(user)
    await log_message(user.id, "customer", "▶️ Started the bot", pending="keep")

    decision = await decide_reply("start", user_id=user.id)
    if decision["send"]:
        msg = decision["message"]
        sent = await deliver(user.id, msg, name=user.first_name, reply_markup=contact_keyboard())
        await record_auto(user.id, msg)
        await log_message(user.id, "bot", sent, rule_name=msg["name"])
        await after_sent(user.id, msg)
    else:
        # The share-number button must always be available
        await message.answer("👇", reply_markup=contact_keyboard())


@dp.message(F.contact, F.chat.type == "private")
async def contact_handler(message: types.Message):
    user = message.from_user
    if message.contact.user_id and message.contact.user_id != user.id:
        await message.answer("🙏 Kripya neeche diye 📱 button se apna khud ka number share karein.",
                             reply_markup=contact_keyboard())
        return

    phone = message.contact.phone_number
    await touch_customer(user)
    topic_id = await get_or_create_topic(user, phone=phone)
    await db_run("UPDATE users SET phone_number = ? WHERE user_id = ?", (phone, user.id))

    if topic_id:
        try:
            await bot.send_message(
                chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id,
                text=f"🔥 NEW VERIFIED LEAD\n👤 Name: {user.first_name}\n📞 Phone: +{phone.lstrip('+')}\n🆔 ID: {user.id}",
            )
            card = await bot.send_contact(
                chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id, phone_number=phone,
                first_name=user.first_name, last_name=user.last_name or "",
            )
            await bot.pin_chat_message(chat_id=ADMIN_GROUP_ID, message_id=card.message_id)
        except Exception as e:
            logging.error(f"Error posting topic lead: {e}")

    await log_message(user.id, "customer", f"📱 Shared Contact: +{phone.lstrip('+')}")
    decision = await decide_reply("contact", user_id=user.id)
    if decision["send"]:
        msg = decision["message"]
        markup = build_buttons(msg) or ReplyKeyboardRemove()
        sent = await deliver(user.id, msg, name=user.first_name, phone=phone, reply_markup=markup)
        await record_auto(user.id, msg)
        await log_message(user.id, "bot", sent, rule_name=msg["name"], pending=0)
        await after_sent(user.id, msg)
    else:
        await message.answer("✅", reply_markup=ReplyKeyboardRemove())


@dp.message(F.photo, F.chat.type == "private")
async def photo_handler(message: types.Message):
    user = message.from_user
    photo = message.photo[-1]
    await touch_customer(user)
    row = await db_one("SELECT phone_number FROM users WHERE user_id = ?", (user.id,))
    phone = (row or {}).get("phone_number") or "Unknown"

    await db_run(
        """INSERT INTO media_vault (user_id, sender_name, phone_number, file_id, utr_code, timestamp, ts)
           VALUES (?, ?, ?, ?, 'Pending Verification', ?, ?)""",
        (user.id, user.first_name, phone, photo.file_id, ist_clock(), now_iso()),
    )

    topic_id = await get_or_create_topic(user, phone=None if phone == "Unknown" else phone)
    if topic_id:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
    await log_message(user.id, "customer", message.caption or "🖼️ Sent Payment Screenshot", media_type="photo")

    decision = await decide_reply("photo", user_id=user.id)
    if decision["send"]:
        msg = decision["message"]
        sent = await deliver(user.id, msg, name=user.first_name, phone=phone)
        await record_auto(user.id, msg)
        await log_message(user.id, "bot", sent, rule_name=msg["name"], pending="keep")
        await topic_note(topic_id, f"🤖 Auto-reply sent · {msg['name']}")
        await after_sent(user.id, msg)


@dp.message(F.chat.type == "private")
async def customer_message_relay(message: types.Message):
    user = message.from_user
    msg_text = message.text or message.caption or "[Media message]"
    await touch_customer(user)

    row = await db_one("SELECT phone_number FROM users WHERE user_id = ?", (user.id,))
    phone = (row or {}).get("phone_number")
    topic_id = await get_or_create_topic(user, phone=phone)
    if topic_id:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
    await log_message(user.id, "customer", msg_text,
                      media_type="text" if message.text else (message.content_type or "media"))

    if not message.text:
        return  # stickers/voice/etc. go to the team only

    decision = await decide_reply("text", message.text, user_id=user.id)
    msg = decision["message"]
    if decision["send"] and msg:
        sent = await deliver(user.id, msg, name=user.first_name, phone=phone or "")
        await record_auto(user.id, msg)
        answered = msg["kind"] == "keywords"
        await log_message(user.id, "bot", sent, rule_name=msg["name"], pending=0 if answered else "keep")
        if answered:
            await topic_note(topic_id, f"🤖 Auto-reply sent · {msg['name']} (matched “{decision['matched']}”)")
        await after_sent(user.id, msg)
    if decision["alert_team"] and (decision["send"] or not msg):
        await topic_note(topic_id, "⚠️ The bot couldn't answer this. Please reply here, or tap a saved reply:",
                         markup=await saved_reply_keyboard(user.id))


# ==============================================================================
# FOLLOW-UP WHEN A CUSTOMER GOES QUIET (runs every minute)
# ==============================================================================
async def run_quiet_check():
    msgs = await list_messages("quiet", active_only=True)
    if not msgs:
        return
    now = datetime.now(timezone.utc)
    longest = max(int(m["quiet_minutes"] or 15) for m in msgs)
    oldest = (now - timedelta(minutes=longest + 120)).isoformat(timespec="seconds")
    users = await db_all(
        "SELECT user_id, first_name, phone_number, topic_id, last_customer_at, last_agent_at "
        "FROM users WHERE last_customer_at >= ?", (oldest,))
    for u in users:
        last_c = parse_iso(u["last_customer_at"])
        last_a = parse_iso(u["last_agent_at"])
        if not last_c or (last_a and last_a > last_c):
            continue  # the team is handling this customer
        quiet_for = (now - last_c).total_seconds() / 60
        for m in msgs:
            mins = int(m["quiet_minutes"] or 15)
            # Only inside a 2-hour window, so old customers aren't messaged after a restart
            if not (mins <= quiet_for <= mins + 120):
                continue
            already = await db_one("SELECT 1 AS x FROM auto_log WHERE user_id = ? AND message_id = ? AND sent_at >= ?",
                                   (u["user_id"], m["id"], u["last_customer_at"]))
            if already:
                continue
            try:
                sent = await deliver(u["user_id"], m, name=u["first_name"], phone=u["phone_number"] or "")
                await record_auto(u["user_id"], m)
                await log_message(u["user_id"], "bot", sent, rule_name=m["name"], pending="keep")
                await topic_note(u["topic_id"], f"⏰ Customer was quiet for {fmt_minutes(mins)} · follow-up sent ({m['name']})")
                await after_sent(u["user_id"], m)
            except Exception as e:
                logging.warning(f"Follow-up to {u['user_id']} failed: {e}")
                await record_auto(u["user_id"], m)  # don't retry a blocked user every minute


async def quiet_loop():
    tick = 0
    while True:
        await asyncio.sleep(15)
        tick += 1
        try:
            await run_scheduled_sends()
        except Exception as e:
            logging.error(f"Scheduled sends failed: {e}")
        if tick % 4 == 0:
            try:
                await run_quiet_check()
            except Exception as e:
                logging.error(f"Quiet check failed: {e}")


# ==============================================================================
# TELEGRAM — ADMIN MENU (buttons, no commands to learn)
# ==============================================================================
class AdminFlow(StatesGroup):
    edit_text = State()
    edit_words = State()
    new_words = State()
    new_text = State()
    new_name = State()
    test = State()


def not_command(message: types.Message) -> bool:
    return not (message.text or "").startswith("/")


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) if not d.startswith("http")
         else InlineKeyboardButton(text=t, url=d) for t, d in row] for row in rows
    ])


CANCEL_KB = [[("❌ Cancel", "am:cancel")]]
MENU_TEXT = ("🤖 Play 246 Bot — Admin Menu\n\n"
             "Choose what you'd like to do. Everything you change here also updates the CRM instantly.")


def menu_kb():
    return kb([
        [("📋 See bot flow", "am:flow"), ("✏️ Edit a message", "am:list:0")],
        [("➕ Add auto-reply", "am:new"), ("📌 Saved replies", "am:saved")],
        [("🧪 Test the bot", "am:test"), ("📊 Stats", "am:stats")],
        [("🌐 Open CRM", CRM_URL)],
    ])


async def safe_edit(message: types.Message, text: str, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


def status_icon(m) -> str:
    return "✅" if m["is_active"] else "⏸"


async def flow_text() -> str:
    msgs = await list_messages()
    by = {k: [m for m in msgs if m["kind"] == k] for k in ALL_KINDS}

    kids = {}
    for m in by["after"]:
        kids.setdefault(m["parent_id"], []).append(m)

    def chain(m, indent="      "):
        out = []
        for c in sorted(kids.get(m["id"], []), key=lambda x: (x["delay_minutes"] or 0, x["id"])):
            out.append(f"{indent}↪️ {delay_label(c['delay_minutes'])}: {status_icon(c)} {c['name']}")
            out += chain(c, indent + "   ")
        return out

    def line(m):
        text = f"{status_icon(m)} {m['name']}"
        extra = chain(m)
        return text + ("\n" + "\n".join(extra) if extra else "")

    parts = ["🤖 YOUR BOT'S CONVERSATION\n"]
    parts.append("1️⃣ Customer starts the bot")
    parts += [f"   → {line(m)}" for m in by["start"]]
    parts.append("\n2️⃣ Customer shares their number")
    parts += [f"   → {line(m)}" for m in by["contact"]]
    parts.append("\n3️⃣ Customer sends a photo")
    parts += [f"   → {line(m)}" for m in by["photo"]]
    parts.append("\n4️⃣ Customer asks something")
    for m in by["keywords"]:
        kws = ", ".join(split_keywords(m["keywords"])[:3])
        parts.append(f"   • “{kws}” → {line(m)}")
    parts += [f"   • Nothing matches → {line(m)} (+ alerts your team)" for m in by["fallback"]]
    parts.append("\n5️⃣ Customer goes quiet")
    if by["quiet"]:
        parts += [f"   • after {fmt_minutes(m['quiet_minutes'])} → {line(m)}" for m in by["quiet"]]
    else:
        parts.append("   • (no follow-up set)")
    parts.append(f"\n📌 Saved replies for your team: {len(by['saved'])}")
    parts += [f"   • {line(m)}" for m in by["saved"] if kids.get(m["id"])]
    handoff = await get_setting("handoff_minutes")
    parts.append(f"\nℹ️ When your team replies, the bot stays quiet for {fmt_minutes(int(handoff or 0))}.")
    return "\n".join(parts)


async def message_detail(m: dict):
    icon = KIND_INFO[m["kind"]][0]
    media = await get_media_row(m.get("media_id"))
    buttons = parse_buttons(m.get("buttons"))
    lines = [
        f"{icon} {m['name']}   {status_icon(m)} {'On' if m['is_active'] else 'Off'}",
        f"🕒 {when_sentence(m, (await get_message(m['parent_id']) or {}).get('name') if m.get('parent_id') else None)}",
        "",
        "Message:",
        m["text"] or "(no text)",
    ]
    if media:
        lines.append(f"\n🖼 Has a {media['kind']} attached")
    if buttons:
        lines.append("🔘 Buttons: " + ", ".join(b["label"] for b in buttons))
    kids = await children_of(m["id"], active_only=False)
    if kids:
        lines.append("\n↪️ Then sends:")
        lines += [f"   • {delay_label(k['delay_minutes'])}: {k['name']}" for k in kids]
    if m.get("use_count"):
        lines.append(f"\n📈 Sent {m['use_count']} times")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "…"

    rows = [[("✏️ Change message", f"am:text:{m['id']}"), ("👁 Preview", f"am:prev:{m['id']}")]]
    if m["kind"] == "keywords":
        rows.append([("🔑 Change words", f"am:words:{m['id']}")])
    if m["kind"] in ("quiet", "after"):
        rows.append([("⏱ Change time", f"am:time:{m['id']}")])
    rows.append([("➕ Add message after this", f"am:aft:{m['id']}")])
    for k in kids[:6]:
        rows.append([(f"↪️ {k['name'][:30]}", f"am:view:{k['id']}")])
    second = [("⏸ Turn off" if m["is_active"] else "▶️ Turn on", f"am:toggle:{m['id']}")]
    if media:
        second.append(("🗑 Remove picture", f"am:rmmedia:{m['id']}"))
    rows.append(second)
    if not m["is_core"]:
        rows.append([("🗑 Delete", f"am:del:{m['id']}")])
    rows.append([("⬅️ Back", "am:list:0"), ("🏠 Menu", "am:home")])
    return text, kb(rows)


async def show_list(message: types.Message, page: int = 0, kind: str = None):
    msgs = await list_messages(kind)
    per = 8
    chunk = msgs[page * per:(page + 1) * per]
    rows = [[(f"{KIND_INFO[m['kind']][0]} {m['name'][:30]} {'' if m['is_active'] else '⏸'}", f"am:view:{m['id']}")]
            for m in chunk]
    nav = []
    if page > 0:
        nav.append(("⬅️ Prev", f"am:list:{page - 1}"))
    if (page + 1) * per < len(msgs):
        nav.append(("Next ➡️", f"am:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("🏠 Menu", "am:home")])
    title = "📌 Saved replies — tap one to edit.\nTo send one to a customer, open their topic and type /r" if kind == "saved" \
        else "✏️ Which message do you want to edit?"
    if kind == "saved":
        rows.insert(-1, [("➕ Add saved reply", "am:newk:saved")])
    await safe_edit(message, title, kb(rows))


async def show_preview(chat_message: types.Message, m: dict, intro="👁 Preview (as customer “Rahul” sees it):"):
    await chat_message.answer(intro)
    try:
        await deliver(chat_message.chat.id, m, name="Rahul", phone="+91 98xxxxxx10",
                      thread_id=chat_message.message_thread_id if chat_message.is_topic_message else None)
    except Exception as e:
        await chat_message.answer(f"Couldn't show preview: {e}")


AFTER_DELAYS = (0, 1, 5, 30, 60, 1440)


def after_delay_rows(prefix: str, back: str):
    labels = [("Right away" if n == 0 else fmt_minutes(n), f"{prefix}:{n}") for n in AFTER_DELAYS]
    return [labels[:3], labels[3:], [("⬅️ Back", back)]]


async def save_pending(data: dict) -> str:
    """Persist what the admin built in the wizard. Returns a confirmation line."""
    ts = now_iso()
    media_id = None
    if data.get("media"):
        media_id = await db_run("INSERT INTO media (kind, file_id, created_at) VALUES (?, ?, ?)",
                                (data["media"]["kind"], data["media"]["file_id"], ts))
    mode = data.get("mode")
    if mode == "edit_text":
        if media_id:
            await db_run("UPDATE bot_messages SET text = ?, media_id = ?, updated_at = ? WHERE id = ?",
                         (data.get("text", ""), media_id, ts, data["msg_id"]))
        else:
            await db_run("UPDATE bot_messages SET text = ?, updated_at = ? WHERE id = ?",
                         (data.get("text", ""), ts, data["msg_id"]))
        return "✅ Saved! The bot will use the new message from now on."
    if mode == "edit_words":
        await db_run("UPDATE bot_messages SET keywords = ?, updated_at = ? WHERE id = ?",
                     (", ".join(data["keywords"]), ts, data["msg_id"]))
        return "✅ Saved! The bot will now reply to these words."
    # New message
    kind = data["kind"]
    if kind == "after" and not data.get("name"):
        parent = await get_message(data["parent_id"])
        data["name"] = f"After {parent['name'] if parent else 'message'}"
    name = data.get("name") or (", ".join(data.get("keywords", [])[:2]).title() if kind == "keywords"
                                 else f"Follow-up after {fmt_minutes(data.get('minutes', 15))}" if kind == "quiet"
                                 else "Saved reply")
    order = (await db_one("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM bot_messages"))["n"]
    await db_run(
        """INSERT INTO bot_messages (name, kind, keywords, quiet_minutes, text, buttons, media_id,
           is_active, is_core, sort_order, parent_id, delay_minutes, skip_if_replied, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, '[]', ?, 1, 0, ?, ?, ?, 1, ?, ?)""",
        (name[:60], kind, ", ".join(data.get("keywords", [])), int(data.get("minutes", 15)),
         data.get("text", ""), media_id, order, data.get("parent_id"), int(data.get("delay", 0)), ts, ts))
    return f"✅ Added “{name}”. It's switched on."


def media_from_message(message: types.Message):
    if message.photo:
        return {"kind": "photo", "file_id": message.photo[-1].file_id}
    if message.animation:
        return {"kind": "animation", "file_id": message.animation.file_id}
    if message.video:
        return {"kind": "video", "file_id": message.video.file_id}
    if message.sticker:
        return {"kind": "sticker", "file_id": message.sticker.file_id}
    return None


async def keyword_conflicts(words: list, exclude_id=None) -> list:
    out = []
    for m in await list_messages("keywords"):
        if m["id"] == exclude_id:
            continue
        theirs = {normalize(k) for k in split_keywords(m["keywords"])}
        for w in words:
            if normalize(w) in theirs:
                out.append(f"“{w}” is already used by “{m['name']}”")
    return out


# ---- wizard inputs (registered BEFORE the topic relay so they are never sent to customers) ----
@dp.message(AdminFlow.edit_text, F.chat.id == ADMIN_GROUP_ID, not_command)
@dp.message(AdminFlow.new_text, F.chat.id == ADMIN_GROUP_ID, not_command)
async def wizard_text_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = message.text or message.caption or ""
    media = media_from_message(message)
    if not text and not media:
        await message.reply("Please send text, or a photo/video with a caption.", reply_markup=kb(CANCEL_KB))
        return
    data.update(text=text, media=media)

    if data.get("mode") == "new" and data.get("kind") == "saved" and not data.get("name"):
        await state.set_data(data)
        await state.set_state(AdminFlow.new_name)
        await message.reply("Last step: give this saved reply a short name for its button.\nExample: Payment verified",
                            reply_markup=kb(CANCEL_KB))
        return

    await state.set_data(data)
    preview = {"text": text, "buttons": "[]", "_media": media}
    if data.get("mode") == "edit_text" and not media:
        existing = await get_message(data["msg_id"])
        if existing:
            preview["buttons"] = existing["buttons"]
            preview["media_id"] = existing["media_id"]
    await show_preview(message, preview, "👁 Here's how it will look:")
    await message.answer("Save this?", reply_markup=kb([[("✅ Save", "am:save"), ("❌ Cancel", "am:cancel")]]))


@dp.message(AdminFlow.new_name, F.chat.id == ADMIN_GROUP_ID, not_command)
async def wizard_name_input(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.reply("Please type a short name.", reply_markup=kb(CANCEL_KB))
        return
    data = await state.get_data()
    data["name"] = name[:60]
    await state.set_data(data)
    await show_preview(message, {"text": data.get("text", ""), "buttons": "[]", "_media": data.get("media")},
                       "👁 Here's how it will look:")
    await message.answer(f"Save “{data['name']}”?", reply_markup=kb([[("✅ Save", "am:save"), ("❌ Cancel", "am:cancel")]]))


@dp.message(AdminFlow.edit_words, F.chat.id == ADMIN_GROUP_ID, not_command)
@dp.message(AdminFlow.new_words, F.chat.id == ADMIN_GROUP_ID, not_command)
async def wizard_words_input(message: types.Message, state: FSMContext):
    words = split_keywords(message.text or "")
    if not words:
        await message.reply("Please send at least one word, separated by commas.\nExample: deposit, jama, paise kaise dalu",
                            reply_markup=kb(CANCEL_KB))
        return
    data = await state.get_data()
    data["keywords"] = words
    clashes = await keyword_conflicts(words, exclude_id=data.get("msg_id"))
    warn = ("\n\n⚠️ Heads up:\n" + "\n".join(clashes) + "\nThe longer/more specific word wins.") if clashes else ""
    await state.set_data(data)
    if data.get("mode") == "edit_words":
        await message.reply(f"New words: {', '.join(words)}{warn}\n\nSave these words?",
                            reply_markup=kb([[("✅ Save", "am:save"), ("❌ Cancel", "am:cancel")]]))
    else:
        await state.set_state(AdminFlow.new_text)
        await message.reply(f"👍 Words: {', '.join(words)}{warn}\n\nNow send the reply the bot should give.\n"
                            "You can send text, or a photo with a caption.\n"
                            "Tip: write [name] where you want the customer's name.",
                            reply_markup=kb(CANCEL_KB))


@dp.message(AdminFlow.test, F.chat.id == ADMIN_GROUP_ID, not_command)
async def wizard_test_input(message: types.Message, state: FSMContext):
    await state.clear()
    decision = await decide_reply("text", message.text or "", simulate=True)
    await message.reply(f"🧪 Customer writes: “{message.text}”\n\n{decision['reason']}")
    if decision["message"] and decision["send"]:
        await show_preview(message, decision["message"], "The bot would reply:")
        for step in await chain_preview(decision["message"]["id"]):
            await message.answer(f"↪️ Then, {step['when'].lower()}: “{step['msg']['name']}”")
    await message.answer("What next?", reply_markup=kb([[("🧪 Test another", "am:test"), ("🏠 Menu", "am:home")]]))


# ---- menu entry points ----
@dp.message(Command("menu", "start", "help"), F.chat.id == ADMIN_GROUP_ID)
async def menu_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    extra = ""
    if message.text and message.text.split()[0].split("@")[0] == "/help":
        extra = ("\n\nOther commands: /r (saved replies, inside a customer topic), /broadcast, "
                 "/export_leads, /stats, /cancel")
    await message.answer(MENU_TEXT + extra, reply_markup=menu_kb())


@dp.message(Command("cancel"), F.chat.id == ADMIN_GROUP_ID)
async def cancel_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("am:"))
async def admin_menu_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None
    msg = callback.message
    await callback.answer()

    if action == "home":
        await state.clear()
        await safe_edit(msg, MENU_TEXT, menu_kb())
    elif action == "cancel":
        await state.clear()
        await safe_edit(msg, "Cancelled — nothing was changed.", menu_kb())
    elif action == "flow":
        await safe_edit(msg, await flow_text(), kb([[("✏️ Edit a message", "am:list:0")], [("🏠 Menu", "am:home")]]))
    elif action == "list":
        await show_list(msg, int(arg or 0))
    elif action == "saved":
        await show_list(msg, 0, kind="saved")
    elif action == "view":
        m = await get_message(int(arg))
        if not m:
            await safe_edit(msg, "That message no longer exists.", menu_kb())
            return
        text, markup = await message_detail(m)
        await safe_edit(msg, text, markup)
    elif action == "prev":
        m = await get_message(int(arg))
        if m:
            await show_preview(msg, m)
    elif action == "text":
        m = await get_message(int(arg))
        if not m:
            await safe_edit(msg, "That message no longer exists.", menu_kb())
            return
        await state.set_state(AdminFlow.edit_text)
        await state.set_data({"mode": "edit_text", "msg_id": int(arg)})
        await safe_edit(msg, f"✏️ Changing “{m['name']}”\n\nSend the new message now.\n"
                             "• Text only → keeps the current picture (if any)\n"
                             "• Photo/video with caption → replaces the picture\n\n"
                             "Tip: write [name] where you want the customer's name.", kb(CANCEL_KB))
    elif action == "words":
        m = await get_message(int(arg))
        if not m:
            await safe_edit(msg, "That message no longer exists.", menu_kb())
            return
        await state.set_state(AdminFlow.edit_words)
        await state.set_data({"mode": "edit_words", "msg_id": int(arg)})
        await safe_edit(msg, f"🔑 Current words: {m['keywords']}\n\nSend the new list of words, separated by commas.",
                        kb(CANCEL_KB))
    elif action == "time" and (await get_message(int(arg)) or {}).get("kind") == "after":
        await safe_edit(msg, "⏱ When should it be sent after the message above?",
                        kb(after_delay_rows(f"am:setdelay:{arg}", f"am:view:{arg}")))
    elif action == "setdelay":
        await db_run("UPDATE bot_messages SET delay_minutes = ?, updated_at = ? WHERE id = ?",
                     (int(parts[3]), now_iso(), int(arg)))
        text, markup = await message_detail(await get_message(int(arg)))
        await safe_edit(msg, "✅ Time updated.\n\n" + text, markup)
    elif action == "aft":
        parent = await get_message(int(arg))
        if not parent:
            await safe_edit(msg, "That message no longer exists.", menu_kb())
            return
        await safe_edit(msg, f"➕ New message after “{parent['name']}”\n\nWhen should it be sent?",
                        kb(after_delay_rows(f"am:aftd:{arg}", f"am:view:{arg}")))
    elif action == "aftd":
        parent = await get_message(int(arg))
        delay = int(parts[3])
        await state.set_state(AdminFlow.new_text)
        await state.set_data({"mode": "new", "kind": "after", "parent_id": int(arg), "delay": delay})
        await safe_edit(msg, f"↪️ {delay_label(delay)} after “{parent['name']}”.\n\n"
                             "Now send the message (text, or a photo with a caption).\n"
                             "If the customer replies before then, it's skipped.\n"
                             "Tip: write [name] where you want the customer's name.", kb(CANCEL_KB))
    elif action == "time":
        rows = [[(fmt_minutes(n), f"am:settime:{arg}:{n}") for n in (5, 15, 30)],
                [(fmt_minutes(n), f"am:settime:{arg}:{n}") for n in (60, 180, 1440)],
                [("⬅️ Back", f"am:view:{arg}")]]
        await safe_edit(msg, "⏱ Send the follow-up after the customer is quiet for:", kb(rows))
    elif action == "settime":
        await db_run("UPDATE bot_messages SET quiet_minutes = ?, updated_at = ? WHERE id = ?",
                     (int(parts[3]), now_iso(), int(arg)))
        m = await get_message(int(arg))
        text, markup = await message_detail(m)
        await safe_edit(msg, "✅ Time updated.\n\n" + text, markup)
    elif action == "toggle":
        await db_run("UPDATE bot_messages SET is_active = 1 - is_active, updated_at = ? WHERE id = ?",
                     (now_iso(), int(arg)))
        m = await get_message(int(arg))
        text, markup = await message_detail(m)
        await safe_edit(msg, text, markup)
    elif action == "rmmedia":
        await db_run("UPDATE bot_messages SET media_id = NULL, updated_at = ? WHERE id = ?", (now_iso(), int(arg)))
        m = await get_message(int(arg))
        text, markup = await message_detail(m)
        await safe_edit(msg, "✅ Picture removed.\n\n" + text, markup)
    elif action == "del":
        m = await get_message(int(arg))
        n_kids = len(await descendants_of(m["id"]))
        await safe_edit(msg, f"🗑 Delete “{m['name']}”?" + (f" This also deletes {n_kids} message(s) sent after it." if n_kids else "") + " This can't be undone here.\n"
                             "(Tip: you can switch it off instead.)",
                        kb([[("Yes, delete", f"am:delyes:{arg}"), ("No, keep it", f"am:view:{arg}")]]))
    elif action == "delyes":
        m = await get_message(int(arg))
        if m and not m["is_core"]:
            kids = await descendants_of(m["id"])
            for k in kids + [m]:
                await db_run("DELETE FROM bot_messages WHERE id = ?", (k["id"],))
            extra = f" and {len(kids)} message(s) after it" if kids else ""
            await safe_edit(msg, f"🗑 Deleted “{m['name']}”{extra}.", menu_kb())
        else:
            await safe_edit(msg, "This message is part of the main flow and can't be deleted. You can switch it off instead.",
                            menu_kb())
    elif action == "new":
        await state.clear()
        await safe_edit(msg, "➕ When should the bot send this new message?", kb([
            [("💬 Customer says certain words", "am:newk:keywords")],
            [("⏰ Customer goes quiet", "am:newk:quiet")],
            [("📌 Saved reply (team sends it)", "am:newk:saved")],
            [("❌ Cancel", "am:cancel")],
        ]))
    elif action == "newk":
        if arg == "keywords":
            await state.set_state(AdminFlow.new_words)
            await state.set_data({"mode": "new", "kind": "keywords"})
            await safe_edit(msg, "💬 Which words should the bot reply to?\n\n"
                                 "Send them separated by commas.\nExample: deposit, jama, paise kaise dalu",
                            kb(CANCEL_KB))
        elif arg == "quiet":
            rows = [[(fmt_minutes(n), f"am:newq:{n}") for n in (5, 15, 30)],
                    [(fmt_minutes(n), f"am:newq:{n}") for n in (60, 180, 1440)],
                    [("❌ Cancel", "am:cancel")]]
            await safe_edit(msg, "⏰ Send the follow-up after the customer is quiet for:", kb(rows))
        else:
            await state.set_state(AdminFlow.new_text)
            await state.set_data({"mode": "new", "kind": "saved"})
            await safe_edit(msg, "📌 Send the saved reply text (or a photo with a caption).\n\n"
                                 "Tip: write [name] where you want the customer's name.", kb(CANCEL_KB))
    elif action == "newq":
        await state.set_state(AdminFlow.new_text)
        await state.set_data({"mode": "new", "kind": "quiet", "minutes": int(arg)})
        await safe_edit(msg, f"⏰ After {fmt_minutes(int(arg))} of silence.\n\nNow send the follow-up message.\n"
                             "Tip: write [name] where you want the customer's name.", kb(CANCEL_KB))
    elif action == "save":
        data = await state.get_data()
        if not data.get("mode"):
            await safe_edit(msg, "Nothing to save — this was already handled.", menu_kb())
            return
        confirmation = await save_pending(data)
        await state.clear()
        await safe_edit(msg, confirmation, kb([[("📋 See bot flow", "am:flow"), ("🏠 Menu", "am:home")]]))
    elif action == "test":
        await state.set_state(AdminFlow.test)
        await safe_edit(msg, "🧪 Type a message the way a customer would.\nExample: bonus kab milega", kb(CANCEL_KB))
    elif action == "stats":
        await safe_edit(msg, await stats_text(), kb([[("🏠 Menu", "am:home")]]))


# ---- saved replies inside a customer's topic ----
@dp.message(Command("r", "replies"), F.chat.id == ADMIN_GROUP_ID)
async def saved_replies_cmd(message: types.Message):
    if not message.is_topic_message or not message.message_thread_id:
        await message.reply("Open a customer's topic and type /r there to send them a saved reply.")
        return
    u = await db_one("SELECT user_id, first_name FROM users WHERE topic_id = ?", (message.message_thread_id,))
    if not u:
        await message.reply("This topic isn't linked to a customer.")
        return
    markup = await saved_reply_keyboard(u["user_id"])
    if not markup:
        await message.reply("No saved replies yet. Add some from /menu → 📌 Saved replies.")
        return
    await message.reply(f"📌 Tap a saved reply to send it to {u['first_name']}:", reply_markup=markup)


@dp.callback_query(F.data.startswith("sr:"))
async def send_saved_callback(callback: CallbackQuery):
    _, msg_id, customer_id = callback.data.split(":")
    m = await get_message(int(msg_id))
    u = await db_one("SELECT user_id, first_name, phone_number FROM users WHERE user_id = ?", (int(customer_id),))
    if not m or not u:
        await callback.answer("Not found", show_alert=True)
        return
    try:
        sent = await deliver(u["user_id"], m, name=u["first_name"], phone=u["phone_number"] or "")
        await record_auto(u["user_id"], m)
        await log_message(u["user_id"], "agent", sent, rule_name=m["name"])
        await after_sent(u["user_id"], m)
        await callback.answer("Sent ✅")
        await safe_edit(callback.message, f"✅ Sent “{m['name']}” to {u['first_name']}:\n\n{sent}")
    except Exception as e:
        await callback.answer(f"Failed: {e}", show_alert=True)


# ---- classic commands (still work, now backed by bot_messages) ----
def kind_from_trigger(trigger: str):
    t = (trigger or "").strip().lower()
    if t.startswith("inactivity"):
        m = re.search(r"(\d+)", t)
        return "quiet", int(m.group(1)) if m else 15, ""
    if t in ("custom", "manual", "saved"):
        return "saved", 15, ""
    if t in ("/start", "start", "contact_shared", "contact"):
        return None, 15, ""
    return "keywords", 15, trigger


@dp.message(Command("view_templates"), F.chat.id == ADMIN_GROUP_ID)
async def view_templates_cmd(message: types.Message):
    await message.answer(await flow_text())
    for m in await list_messages():
        await message.answer(f"ID {m['id']} · {status_icon(m)} {m['name']}\n🕒 {when_sentence(m)}\n\n{m['text']}\n\n"
                             f"Change it: /edit_step {m['id']} <new text>   (or use /menu)")
        await asyncio.sleep(0.3)


@dp.message(Command("set_welcome"), F.chat.id == ADMIN_GROUP_ID)
async def set_welcome_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /set_welcome <new welcome text>\nTip: use /menu → ✏️ Edit a message for an easier way.")
        return
    await db_run("UPDATE bot_messages SET text = ?, updated_at = ? WHERE kind = 'start'", (parts[1], now_iso()))
    await message.reply(f"✅ Welcome message updated.\n\n{parts[1]}")


@dp.message(Command("set_links"), F.chat.id == ADMIN_GROUP_ID)
async def set_links_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /set_links <new site links text>")
        return
    await db_run("UPDATE bot_messages SET text = ?, updated_at = ? WHERE kind = 'contact'", (parts[1], now_iso()))
    await message.reply(f"✅ Site links message updated.\n\n{parts[1]}")


@dp.message(Command("add_step"), F.chat.id == ADMIN_GROUP_ID)
async def add_step_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    segs = [s.strip() for s in parts[1].split("|", 2)] if len(parts) > 1 else []
    if len(segs) < 3:
        await message.reply("Usage: /add_step Title | words or inactivity_15m or saved | Message\n"
                            "Easier: /menu → ➕ Add auto-reply")
        return
    title, trigger, content = segs
    kind, minutes, words = kind_from_trigger(trigger)
    if not kind:
        await message.reply("The welcome and site-links messages already exist — edit them with /menu.")
        return
    await save_pending({"mode": "new", "kind": kind, "name": title, "minutes": minutes,
                        "keywords": split_keywords(words), "text": content})
    await message.reply(f"✅ Added “{title}”.")


@dp.message(Command("edit_step"), F.chat.id == ADMIN_GROUP_ID)
async def edit_step_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply("Usage: /edit_step <ID> <new text>   (IDs: /view_templates)")
        return
    if not await get_message(int(parts[1])):
        await message.reply("Message not found. Use /view_templates to see IDs.")
        return
    await db_run("UPDATE bot_messages SET text = ?, updated_at = ? WHERE id = ?", (parts[2], now_iso(), int(parts[1])))
    await message.reply(f"✅ Message {parts[1]} updated.")


@dp.message(Command("delete_step"), F.chat.id == ADMIN_GROUP_ID)
async def delete_step_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply("Usage: /delete_step <ID>")
        return
    m = await get_message(int(parts[1]))
    if not m:
        await message.reply("Message not found.")
    elif m["is_core"]:
        await message.reply("This is part of the main flow and can't be deleted. Switch it off from /menu instead.")
    else:
        await db_run("DELETE FROM bot_messages WHERE id = ?", (m["id"],))
        await message.reply(f"🗑 Deleted “{m['name']}”.")


@dp.message(Command("set_trigger"), F.chat.id == ADMIN_GROUP_ID)
async def set_trigger_cmd(message: types.Message):
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply("Usage: /set_trigger <ID> <words, or inactivity_30m, or saved>")
        return
    m = await get_message(int(parts[1]))
    kind, minutes, words = kind_from_trigger(parts[2])
    if not m or m["is_core"] or not kind:
        await message.reply("Can't change that one. Use /menu to edit messages.")
        return
    await db_run("UPDATE bot_messages SET kind = ?, quiet_minutes = ?, keywords = ?, updated_at = ? WHERE id = ?",
                 (kind, minutes, words, now_iso(), m["id"]))
    await message.reply(f"✅ Updated: {when_sentence(await get_message(m['id']))}")


async def compute_stats() -> dict:
    today = ist_midnight_utc_iso()
    one = lambda sql, a=(): db_one(sql, a)
    return {
        "total_leads": (await one("SELECT COUNT(*) n FROM users"))["n"],
        "verified_leads": (await one("SELECT COUNT(*) n FROM users WHERE phone_number IS NOT NULL"))["n"],
        "pending_replies": (await one("SELECT COUNT(*) n FROM users WHERE is_pending = 1"))["n"],
        "total_messages": (await one("SELECT COUNT(*) n FROM messages"))["n"],
        "vault_count": (await one("SELECT COUNT(DISTINCT user_id) n FROM media_vault"))["n"],
        "unpaid": (await one("SELECT COUNT(*) n FROM users WHERE phone_number IS NOT NULL AND user_id NOT IN "
                             "(SELECT DISTINCT user_id FROM media_vault)"))["n"],
        "new_today": (await one("SELECT COUNT(*) n FROM users WHERE created_at >= ?", (today,)))["n"],
        "auto_today": (await one("SELECT COUNT(*) n FROM auto_log WHERE sent_at >= ? AND kind != 'saved'", (today,)))["n"],
        "unanswered_today": (await one("SELECT COUNT(*) n FROM auto_log WHERE sent_at >= ? AND kind = 'fallback'", (today,)))["n"],
    }


async def stats_text() -> str:
    s = await compute_stats()
    return (
        "📊 PLAY 246 LIVE STATS\n\n"
        f"Total leads: {s['total_leads']}  (+{s['new_today']} today)\n"
        f"Shared phone: {s['verified_leads']}\n"
        f"Waiting for reply: {s['pending_replies']}\n"
        f"Sent screenshot: {s['vault_count']}\n"
        f"No screenshot yet: {s['unpaid']}\n\n"
        f"🤖 Auto-replies today: {s['auto_today']}\n"
        f"🤷 Bot couldn't answer today: {s['unanswered_today']}\n\n"
        f"CRM: {CRM_URL}"
    )


@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def stats_cmd(message: types.Message):
    await message.reply(await stats_text())


AUDIENCE_SQL = {
    "verified": "SELECT user_id, topic_id FROM users WHERE phone_number IS NOT NULL",
    "pending": "SELECT user_id, topic_id FROM users WHERE phone_number IS NULL",
    "unpaid": "SELECT user_id, topic_id FROM users WHERE phone_number IS NOT NULL AND user_id NOT IN "
              "(SELECT DISTINCT user_id FROM media_vault)",
    "all": "SELECT user_id, topic_id FROM users",
}


@dp.message(Command("broadcast"), F.chat.id == ADMIN_GROUP_ID)
async def broadcast_cmd(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="All Users", callback_data="bc_all"),
         InlineKeyboardButton(text="Verified", callback_data="bc_verified")],
        [InlineKeyboardButton(text="Pending", callback_data="bc_pending"),
         InlineKeyboardButton(text="Unpaid (No SS)", callback_data="bc_unpaid")],
    ])
    await message.reply("📢 Who should receive this broadcast?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("bc_"))
async def broadcast_audience_callback(callback: CallbackQuery):
    audience_map = {"bc_all": ("all", "All Users"), "bc_verified": ("verified", "Verified Leads"),
                    "bc_pending": ("pending", "Pending / Unverified"), "bc_unpaid": ("unpaid", "Unpaid (No Screenshot)")}
    key, label = audience_map.get(callback.data, ("all", "All Users"))
    await callback.message.edit_text(
        f"Audience: {label}\n\nNow send your broadcast (text, photo, sticker or any media) as a REPLY to this message.")
    pending_broadcasts[callback.message.message_id] = key
    await callback.answer(f"Audience: {label}")


@dp.message(Command("export_leads"), F.chat.id == ADMIN_GROUP_ID)
async def export_leads_cmd(message: types.Message):
    csv_bytes, count = await leads_csv()
    if not count:
        await message.reply("No leads found.")
        return
    file = BufferedInputFile(csv_bytes, filename=f"Play246_Leads_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv")
    await message.reply_document(file, caption=f"Exported {count} leads.")


async def leads_csv():
    rows = await db_all("SELECT user_id, first_name, username, phone_number, created_at, last_time FROM users ORDER BY user_id")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "first_name", "username", "phone_number", "first_seen_utc", "last_seen"])
    for r in rows:
        w.writerow([r["user_id"], r["first_name"], r["username"], r["phone_number"] or "", r["created_at"] or "", r["last_time"] or ""])
    return buf.getvalue().encode("utf-8-sig"), len(rows)


# In-memory store for pending broadcast audience selection
pending_broadcasts: dict = {}


@dp.message(F.chat.id == ADMIN_GROUP_ID, F.reply_to_message)
async def handle_broadcast_reply(message: types.Message):
    reply_to_id = message.reply_to_message.message_id
    if reply_to_id not in pending_broadcasts:
        return await _admin_relay(message)  # normal reply inside a customer topic

    audience = pending_broadcasts.pop(reply_to_id)
    users = await db_all(AUDIENCE_SQL.get(audience, AUDIENCE_SQL["all"]))
    delivered = failed = 0
    for u in users:
        try:
            await message.copy_to(chat_id=u["user_id"])
            delivered += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await message.reply(f"✅ Broadcast finished!\n\nAudience: {audience.title()}\nDelivered: {delivered}\nFailed / Blocked: {failed}")


@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply_relay(message: types.Message):
    await _admin_relay(message)


async def _admin_relay(message: types.Message):
    """Forward a team member's message in a customer topic to that customer."""
    if message.forum_topic_created or message.is_automatic_forward:
        return
    if message.from_user and message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return
    if not message.message_thread_id:
        return
    u = await db_one("SELECT user_id FROM users WHERE topic_id = ?", (message.message_thread_id,))
    if not u:
        return
    try:
        await message.copy_to(chat_id=u["user_id"])
        await log_message(u["user_id"], "agent", message.text or message.caption or "[Team media]",
                          media_type="text" if message.text else (message.content_type or "media"))
    except Exception as e:
        await message.reply(f"Delivery failed: {e}")


# ==============================================================================
# CRM WEB SERVER + REST API
# ==============================================================================
SESSION_COOKIE = "p246_session"


def session_token() -> str:
    return hmac.new(f"{CRM_PASSWORD}|{BOT_TOKEN}".encode(), b"play246-crm-session", hashlib.sha256).hexdigest()


def is_logged_in(request) -> bool:
    if not CRM_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), session_token())


@web.middleware
async def api_middleware(request, handler):
    path = request.path
    if path.startswith("/api/") and path not in ("/api/login", "/api/me") and not is_logged_in(request):
        return web.json_response({"error": "Please log in again."}, status=401)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        if not path.startswith("/api/"):
            raise
        logging.exception(f"API error on {path}")
        return web.json_response({"error": friendly_error(e)}, status=500)


def friendly_error(e: Exception) -> str:
    s = str(e)
    if "bot was blocked" in s.lower():
        return "This customer has blocked the bot, so the message couldn't be delivered."
    if "chat not found" in s.lower():
        return "Telegram couldn't find this customer's chat."
    return s or e.__class__.__name__


app = web.Application(middlewares=[api_middleware], client_max_size=25 * 1024 * 1024)


async def serve_index(request):
    if os.path.exists(CRM_HTML_PATH):
        with open(CRM_HTML_PATH, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html", headers={"Cache-Control": "no-store"})
    return web.Response(text="CRM HTML not found", status=404)


FONTS_DIR = os.path.join(BASE_DIR, "static", "fonts")


async def serve_font(request):
    """Self-hosted CRM fonts (no CDN). Only plain .woff2 file names inside static/fonts are served."""
    name = request.match_info["name"]
    path = os.path.join(FONTS_DIR, name)
    if not re.fullmatch(r"[a-z0-9-]+\.woff2", name) or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "font/woff2", "Cache-Control": "public, max-age=31536000, immutable"})


async def api_me(request):
    return web.json_response({"auth_required": bool(CRM_PASSWORD), "logged_in": is_logged_in(request),
                              "bot_username": "prototypetelegram247bot"})


async def api_login(request):
    data = await request.json()
    if not CRM_PASSWORD:
        return web.json_response({"ok": True})
    if not hmac.compare_digest(str(data.get("password", "")), CRM_PASSWORD):
        await asyncio.sleep(1)
        return web.json_response({"error": "That password isn't right."}, status=401)
    resp = web.json_response({"ok": True})
    secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
    resp.set_cookie(SESSION_COOKIE, session_token(), max_age=30 * 86400, httponly=True, samesite="Lax", secure=secure)
    return resp


async def api_logout(request):
    resp = web.json_response({"ok": True})
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def api_stats(request):
    return web.json_response(await compute_stats())


async def api_chats(request):
    rows = await db_all("""SELECT user_id, first_name, username, phone_number, is_pending, last_message, last_time,
                                  last_customer_at, last_agent_at, created_at, topic_id
                           FROM users ORDER BY is_pending DESC,
                                COALESCE(last_customer_at, created_at, '') DESC, user_id DESC""")
    paid = {r["user_id"] for r in await db_all("SELECT DISTINCT user_id FROM media_vault")}
    # Latest payment screenshot per customer, so Chats can show and verify it in place
    latest_pay = {r["user_id"]: r for r in await db_all(
        """SELECT v.user_id, v.id, v.utr_code FROM media_vault v
           JOIN (SELECT user_id, MAX(id) AS id FROM media_vault GROUP BY user_id) m ON m.id = v.id""")}
    handoff = int(await get_setting("handoff_minutes") or 0)
    now = datetime.now(timezone.utc)

    def paused_minutes(last_agent_at):
        """Minutes left before the bot answers again after a team reply (0 = bot is answering)."""
        last = parse_iso(last_agent_at)
        if not handoff or not last:
            return 0
        left = timedelta(minutes=handoff) - (now - last)
        return max(0, -(-int(left.total_seconds()) // 60))

    return web.json_response([{
        "user_id": r["user_id"],
        "first_name": r["first_name"] or "Player",
        "username": r["username"] or "",
        "phone_number": r["phone_number"] or "",
        "is_pending": r["is_pending"] or 0,
        "last_message": r["last_message"] or "Started conversation",
        "last_time": r["last_time"] or "",
        "last_at": r["last_customer_at"] or r["created_at"] or "",
        "has_screenshot": r["user_id"] in paid,
        "has_topic": bool(r["topic_id"]),
        "bot_paused_minutes": paused_minutes(r["last_agent_at"]),
        "payment": ({"id": latest_pay[r["user_id"]]["id"],
                     "status": latest_pay[r["user_id"]]["utr_code"] or "Pending Verification",
                     "image_url": f"/api/vault/{latest_pay[r['user_id']]['id']}/image"}
                    if r["user_id"] in latest_pay else None),
    } for r in rows])


async def api_chat_messages(request):
    user_id = int(request.match_info["user_id"])
    rows = await db_all("SELECT sender, text, media_type, timestamp, ts, rule_name FROM messages "
                        "WHERE user_id = ? ORDER BY id ASC", (user_id,))
    return web.json_response(rows)


async def api_mark_read(request):
    user_id = int(request.match_info["user_id"])
    await db_run("UPDATE users SET is_pending = 0 WHERE user_id = ?", (user_id,))
    return web.json_response({"ok": True})


async def api_reply_customer(request):
    user_id = int(request.match_info["user_id"])
    file_bytes = file_name = None
    if request.content_type.startswith("multipart/"):
        reader = await request.post()
        text = (reader.get("text") or "").strip()
        f = reader.get("file")
        if f is not None and hasattr(f, "file"):
            file_bytes, file_name = f.file.read(), f.filename
    else:
        text = ((await request.json()).get("text") or "").strip()
    if not text and not file_bytes:
        return web.json_response({"error": "Type a message or attach a photo first."}, status=400)

    u = await db_one("SELECT topic_id FROM users WHERE user_id = ?", (user_id,))
    topic_id = u["topic_id"] if u else None
    if file_bytes:
        await bot.send_photo(chat_id=user_id, photo=BufferedInputFile(file_bytes, filename=file_name or "reply.jpg"),
                             caption=text or None)
        if topic_id:
            try:
                await bot.send_photo(ADMIN_GROUP_ID, BufferedInputFile(file_bytes, filename=file_name or "reply.jpg"),
                                     message_thread_id=topic_id, caption=f"👤 [CRM reply]\n{text}")
            except Exception:
                pass
        await log_message(user_id, "agent", text or "🖼️ Sent a photo", media_type="photo")
    else:
        await bot.send_message(chat_id=user_id, text=text)
        await topic_note(topic_id, f"👤 [CRM reply]\n{text}")
        await log_message(user_id, "agent", text)
    return web.json_response({"status": "sent"})


async def api_send_saved(request):
    user_id = int(request.match_info["user_id"])
    data = await request.json()
    m = await get_message(int(data.get("message_id", 0)))
    u = await db_one("SELECT first_name, phone_number, topic_id FROM users WHERE user_id = ?", (user_id,))
    if not m or not u:
        return web.json_response({"error": "Not found"}, status=404)
    sent = await deliver(user_id, m, name=u["first_name"], phone=u["phone_number"] or "")
    await record_auto(user_id, m)
    await log_message(user_id, "agent", sent, rule_name=m["name"])
    await after_sent(user_id, m)
    await topic_note(u["topic_id"], f"👤 [CRM] Saved reply sent · {m['name']}")
    return web.json_response({"status": "sent"})


# ---- bot messages ----
async def serialize_message(m: dict) -> dict:
    media = await get_media_row(m.get("media_id"))
    return {
        "id": m["id"], "name": m["name"], "kind": m["kind"],
        "keywords": split_keywords(m["keywords"]), "match_mode": m.get("match_mode") or "contains",
        "quiet_minutes": m.get("quiet_minutes") or 15, "text": m["text"] or "",
        "buttons": parse_buttons(m.get("buttons")),
        "media": {"id": media["id"], "kind": media["kind"], "url": f"/api/media/{media['id']}"} if media else None,
        "is_active": bool(m["is_active"]), "is_core": bool(m["is_core"]),
        "sort_order": m.get("sort_order") or 0, "use_count": m.get("use_count") or 0,
        "last_used_at": m.get("last_used_at"), "updated_at": m.get("updated_at"),
        "parent_id": m.get("parent_id"), "delay_minutes": int(m.get("delay_minutes") or 0),
        "skip_if_replied": bool(m.get("skip_if_replied", 1)),
        "when": when_sentence(m, (await get_message(m["parent_id"]) or {}).get("name") if m.get("parent_id") else None),
    }


def clean_payload(data: dict, existing: dict = None) -> tuple:
    """Validate CRM input. Returns (fields, error)."""
    kind = existing["kind"] if existing else data.get("kind")
    if kind not in ALL_KINDS:
        return None, "Please choose when the bot should send this."
    fields = {}
    if "name" in data or not existing:
        name = str(data.get("name") or "").strip()
        if not name:
            name = KIND_INFO[kind][1] if kind != "keywords" else ", ".join(split_keywords(data.get("keywords"))[:2]).title()
        fields["name"] = name[:60] or "New message"
    if "text" in data:
        fields["text"] = str(data.get("text") or "")[:4000]
    if "keywords" in data:
        kws = split_keywords(data.get("keywords"))
        if kind == "keywords" and not kws:
            return None, "Add at least one word the customer might type."
        fields["keywords"] = ", ".join(kws)
    elif kind == "keywords" and not existing:
        return None, "Add at least one word the customer might type."
    if "match_mode" in data:
        fields["match_mode"] = "exact" if data.get("match_mode") == "exact" else "contains"
    if "quiet_minutes" in data:
        try:
            mins = int(data.get("quiet_minutes"))
        except (TypeError, ValueError):
            return None, "Please choose how long to wait."
        if not 1 <= mins <= 10080:
            return None, "The wait must be between 1 minute and 7 days."
        fields["quiet_minutes"] = mins
    if "buttons" in data:
        raw = data.get("buttons") or []
        for b in raw:
            if str(b.get("label", "")).strip() and not re.match(r"^(https?://|tg://)", str(b.get("url", "")).strip(), re.I):
                return None, f"The button “{b.get('label')}” needs a link starting with https://"
        fields["buttons"] = json.dumps(parse_buttons(raw), ensure_ascii=False)
    if "delay_minutes" in data:
        try:
            delay = int(data.get("delay_minutes") or 0)
        except (TypeError, ValueError):
            return None, "Please choose when to send it."
        if not 0 <= delay <= 10080:
            return None, "The wait must be between 0 minutes and 7 days."
        fields["delay_minutes"] = delay
    if "skip_if_replied" in data:
        fields["skip_if_replied"] = 1 if data.get("skip_if_replied") else 0
    if "is_active" in data:
        fields["is_active"] = 1 if data.get("is_active") else 0
    if "sort_order" in data:
        fields["sort_order"] = int(data.get("sort_order") or 0)
    return fields, None


async def api_list_messages(request):
    return web.json_response([await serialize_message(m) for m in await list_messages()])


async def api_create_message(request):
    data = await request.json()
    if data.get("kind") in CORE_KINDS:
        return web.json_response({"error": "That message already exists in your main flow — edit it instead."}, status=400)
    fields, err = clean_payload(data)
    if err:
        return web.json_response({"error": err}, status=400)
    if data["kind"] == "after":
        if not await get_message(int(data.get("parent_id") or 0)):
            return web.json_response({"error": "The message this should follow no longer exists."}, status=400)
        fields["parent_id"] = int(data["parent_id"])
    if not (fields.get("text") or "").strip() and not data.get("media_id") and not data.get("media_pending"):
        return web.json_response({"error": "Write the message the bot should send."}, status=400)
    ts = now_iso()
    fields.setdefault("sort_order", (await db_one("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM bot_messages"))["n"])
    fields.update(kind=data["kind"], is_core=0, created_at=ts, updated_at=ts)
    if data.get("media_id"):
        fields["media_id"] = int(data["media_id"])
    cols = ", ".join(fields)
    new_id = await db_run(f"INSERT INTO bot_messages ({cols}) VALUES ({', '.join('?' * len(fields))})",
                          list(fields.values()))
    return web.json_response(await serialize_message(await get_message(new_id)))


async def api_update_message(request):
    msg_id = int(request.match_info["id"])
    existing = await get_message(msg_id)
    if not existing:
        return web.json_response({"error": "This message no longer exists."}, status=404)
    data = await request.json()
    fields, err = clean_payload(data, existing)
    if err:
        return web.json_response({"error": err}, status=400)
    if "text" in fields and not fields["text"].strip() and not existing.get("media_id"):
        return web.json_response({"error": "The message can't be empty."}, status=400)
    if fields:
        fields["updated_at"] = now_iso()
        await db_run(f"UPDATE bot_messages SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     [*fields.values(), msg_id])
    return web.json_response(await serialize_message(await get_message(msg_id)))


async def api_delete_message(request):
    msg_id = int(request.match_info["id"])
    m = await get_message(msg_id)
    if not m:
        return web.json_response({"error": "Already deleted."}, status=404)
    if m["is_core"]:
        return web.json_response({"error": "This is part of the main flow, so it can't be deleted. You can switch it off instead."},
                                 status=400)
    kids = await descendants_of(msg_id)
    deleted = [await serialize_message(x) for x in [m] + kids]   # serialize before deleting (needs parent names)
    for x in [m] + kids:
        await db_run("DELETE FROM bot_messages WHERE id = ?", (x["id"],))
        await db_run("DELETE FROM scheduled_sends WHERE message_id = ?", (x["id"],))
    return web.json_response({"deleted": deleted[0], "children": deleted[1:]})


async def api_restore_message(request):
    """Undo a delete: re-insert the message (and the messages that came after it)."""
    body = await request.json()
    items = [body["deleted"], *body.get("children", [])] if "deleted" in body else [body]
    ts = now_iso()
    id_map, first = {}, None
    for d in items:
        parent = id_map.get(d.get("parent_id"), d.get("parent_id"))
        new_id = await db_run(
            """INSERT INTO bot_messages (name, kind, keywords, match_mode, quiet_minutes, text, buttons, media_id,
               is_active, is_core, sort_order, use_count, parent_id, delay_minutes, skip_if_replied, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
            (d["name"], d["kind"], ", ".join(d.get("keywords") or []), d.get("match_mode", "contains"),
             d.get("quiet_minutes", 15), d.get("text", ""), json.dumps(d.get("buttons") or [], ensure_ascii=False),
             (d.get("media") or {}).get("id"), 1 if d.get("is_active") else 0, d.get("sort_order", 100),
             d.get("use_count", 0), parent, int(d.get("delay_minutes") or 0), 1 if d.get("skip_if_replied", True) else 0, ts, ts))
        id_map[d["id"]] = new_id
        first = first or new_id
    return web.json_response(await serialize_message(await get_message(first)))


async def api_upload_media(request):
    msg_id = int(request.match_info["id"])
    if not await get_message(msg_id):
        return web.json_response({"error": "This message no longer exists."}, status=404)
    reader = await request.post()
    f = reader.get("file")
    if f is None or not hasattr(f, "file"):
        return web.json_response({"error": "Choose a picture or video first."}, status=400)
    data = f.file.read()
    mime = f.content_type or mimetypes.guess_type(f.filename or "")[0] or ""
    if mime == "image/gif":
        kind = "animation"
    elif mime.startswith("image/"):
        kind = "photo"
    elif mime.startswith("video/"):
        kind = "video"
    else:
        return web.json_response({"error": "Only pictures, GIFs and videos can be attached."}, status=400)
    limit = 10 if kind == "photo" else 20
    if len(data) > limit * 1024 * 1024:
        return web.json_response({"error": f"That file is too big (max {limit} MB)."}, status=400)
    media_id = await db_run("INSERT INTO media (kind, data, mime, filename, created_at) VALUES (?, ?, ?, ?, ?)",
                            (kind, data, mime, f.filename, now_iso()))
    await db_run("UPDATE bot_messages SET media_id = ?, updated_at = ? WHERE id = ?", (media_id, now_iso(), msg_id))
    return web.json_response(await serialize_message(await get_message(msg_id)))


async def api_remove_media(request):
    msg_id = int(request.match_info["id"])
    await db_run("UPDATE bot_messages SET media_id = NULL, updated_at = ? WHERE id = ?", (now_iso(), msg_id))
    return web.json_response(await serialize_message(await get_message(msg_id)))


async def _media_bytes(media_id: int = None, file_id: str = None):
    """Return (bytes, mime). Downloads from Telegram (and caches) when we only have a file_id."""
    if media_id:
        row = await db_one("SELECT kind, file_id, data, mime, filename FROM media WHERE id = ?", (media_id,))
        if not row:
            return None, None
        if row["data"] is not None:
            return row["data"], row["mime"] or "application/octet-stream"
        file_id = row["file_id"]
    if not file_id:
        return None, None
    tg_file = await bot.get_file(file_id)
    buf = await bot.download_file(tg_file.file_path)
    data = buf.read()
    mime = mimetypes.guess_type(tg_file.file_path or "")[0] or "image/jpeg"
    if tg_file.file_path and tg_file.file_path.endswith(".webp"):
        mime = "image/webp"
    if media_id and len(data) < 10 * 1024 * 1024:
        await db_run("UPDATE media SET data = ?, mime = ? WHERE id = ?", (data, mime, media_id))
    return data, mime


async def api_media(request):
    data, mime = await _media_bytes(media_id=int(request.match_info["id"]))
    if data is None:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type=mime.split(";")[0], headers={"Cache-Control": "private, max-age=86400"})


async def api_simulate(request):
    d = await request.json()
    event = d.get("event") or "text"
    decision = await decide_reply(event, d.get("text") or "", simulate=True)
    m = decision["message"]
    return web.json_response({
        "reason": decision["reason"],
        "matched": decision["matched"],
        "will_send": decision["send"],
        "alert_team": decision["alert_team"],
        "message": await serialize_message(m) if m else None,
        "preview_text": render_text(m["text"], "Rahul", "+91 98xxxxxx10") if m else None,
        "then": [{"when": st["when"], "minutes": st["minutes"], "message": await serialize_message(st["msg"]),
                  "preview_text": render_text(st["msg"]["text"], "Rahul", "+91 98xxxxxx10")}
                 for st in (await chain_preview(m["id"]) if m and decision["send"] else [])],
    })


async def api_get_settings(request):
    return web.json_response({k: await get_setting(k) for k in DEFAULT_SETTINGS})


async def api_put_settings(request):
    d = await request.json()
    if "handoff_minutes" in d:
        await set_setting("handoff_minutes", max(0, min(1440, int(d["handoff_minutes"] or 0))))
    if "alert_team" in d:
        await set_setting("alert_team", "1" if d["alert_team"] else "0")
    return await api_get_settings(request)


# ---- bot display name (the name customers see at the top of the Telegram chat) ----
BOT_NAME_MAX = 64  # Telegram's limit for setMyName


def _telegram_error(e: Exception) -> str:
    if isinstance(e, TelegramRetryAfter):
        wait = max(1, int(e.retry_after))
        when = f"{wait} seconds" if wait < 120 else f"{-(-wait // 60)} minutes"
        return f"Telegram only allows a few name changes at a time. Try again in {when}."
    return "Couldn't reach Telegram. Check the bot token and your connection, then try again."


async def api_get_bot_name(request):
    try:
        name = (await bot.get_my_name()).name
    except Exception as e:
        logging.warning("get_my_name failed: %s", e)
        return web.json_response({"error": _telegram_error(e)}, status=502)
    return web.json_response({"name": name, "max": BOT_NAME_MAX})


async def api_put_bot_name(request):
    d = await request.json()
    name = " ".join(str(d.get("name") or "").split())
    if not name:
        return web.json_response({"error": "Type a name for the bot first."}, status=400)
    if len(name) > BOT_NAME_MAX:
        return web.json_response({"error": f"Keep the name to {BOT_NAME_MAX} characters or fewer."}, status=400)
    try:
        await bot.set_my_name(name=name)
    except Exception as e:
        logging.warning("set_my_name failed: %s", e)
        return web.json_response({"error": _telegram_error(e)}, status=502)
    return web.json_response({"name": name, "max": BOT_NAME_MAX})


# ---- payments vault ----
async def api_get_vault(request):
    rows = await db_all("SELECT id, user_id, sender_name, phone_number, utr_code, timestamp, ts FROM media_vault ORDER BY id DESC")
    return web.json_response([{**r, "status": r["utr_code"] or "Pending Verification",
                               "image_url": f"/api/vault/{r['id']}/image"} for r in rows])


async def api_vault_image(request):
    row = await db_one("SELECT file_id FROM media_vault WHERE id = ?", (int(request.match_info["id"]),))
    if not row:
        raise web.HTTPNotFound()
    data, mime = await _media_bytes(file_id=row["file_id"])
    if data is None:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type=mime, headers={"Cache-Control": "private, max-age=86400"})


async def api_vault_update(request):
    vid = int(request.match_info["id"])
    status = str((await request.json()).get("status") or "").strip()[:60]
    if not status:
        return web.json_response({"error": "Choose a status."}, status=400)
    await db_run("UPDATE media_vault SET utr_code = ? WHERE id = ?", (status, vid))
    return web.json_response({"ok": True, "status": status})


# ---- broadcast ----
async def api_audience_counts(request):
    out = {}
    for key, sql in AUDIENCE_SQL.items():
        out[key] = (await db_one(f"SELECT COUNT(*) AS n FROM ({sql}) AS sub"))["n"]
    return web.json_response(out)


async def api_broadcast(request):
    file_bytes = file_name = None
    if request.content_type.startswith("multipart/"):
        reader = await request.post()
        audience = reader.get("audience", "all")
        text = (reader.get("text") or "").strip()
        f = reader.get("file")
        if f is not None and hasattr(f, "file"):
            file_bytes, file_name = f.file.read(), f.filename
    else:
        data = await request.json()
        audience = data.get("audience", "all")
        text = (data.get("text") or "").strip()
    if not text and not file_bytes:
        return web.json_response({"error": "Write a message or attach a picture first."}, status=400)

    users = await db_all(AUDIENCE_SQL.get(audience, AUDIENCE_SQL["all"]))
    delivered = failed = 0
    photo_id = None
    for u in users:
        try:
            if file_bytes:
                photo = photo_id or BufferedInputFile(file_bytes, filename=file_name or "poster.jpg")
                sent = await bot.send_photo(chat_id=u["user_id"], photo=photo, caption=text or None)
                photo_id = photo_id or (sent.photo[-1].file_id if sent.photo else None)
            else:
                await bot.send_message(chat_id=u["user_id"], text=text)
            if u["topic_id"]:
                await topic_note(u["topic_id"], f"📢 [Broadcast sent]\n{text or '(picture)'}")
            delivered += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    return web.json_response({"delivered": delivered, "failed": failed})


async def api_export_leads(request):
    data, _ = await leads_csv()
    return web.Response(body=data, content_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="Play246_Leads_{datetime.now(IST).strftime("%Y%m%d_%H%M")}.csv"'})


async def health_check(request):
    try:
        await db_one("SELECT 1 AS ok")
        db_ok = "ok"
    except Exception as e:
        db_ok = f"error: {e}"
    return web.json_response({"status": "alive", "bot": "Play 246 Support Bot", "db": db_ok,
                              "database": "postgres" if USE_PG else "sqlite",
                              "telegram": "polling" if BOT_POLLING else "crm-only"})


r = app.router
r.add_get("/health", health_check)
r.add_get("/", serve_index)
r.add_get("/fonts/{name}", serve_font)
r.add_get("/api/me", api_me)
r.add_post("/api/login", api_login)
r.add_post("/api/logout", api_logout)
r.add_get("/api/stats", api_stats)
r.add_get("/api/chats", api_chats)
r.add_get("/api/chats/{user_id}/messages", api_chat_messages)
r.add_post("/api/chats/{user_id}/read", api_mark_read)
r.add_post("/api/chats/{user_id}/reply", api_reply_customer)
r.add_post("/api/chats/{user_id}/send-saved", api_send_saved)
r.add_get("/api/bot-messages", api_list_messages)
r.add_post("/api/bot-messages", api_create_message)
r.add_post("/api/bot-messages/restore", api_restore_message)
r.add_put("/api/bot-messages/{id}", api_update_message)
r.add_delete("/api/bot-messages/{id}", api_delete_message)
r.add_post("/api/bot-messages/{id}/media", api_upload_media)
r.add_delete("/api/bot-messages/{id}/media", api_remove_media)
r.add_get("/api/media/{id}", api_media)
r.add_post("/api/simulate", api_simulate)
r.add_get("/api/settings", api_get_settings)
r.add_put("/api/settings", api_put_settings)
r.add_get("/api/bot-name", api_get_bot_name)
r.add_put("/api/bot-name", api_put_bot_name)
r.add_get("/api/vault", api_get_vault)
r.add_get("/api/vault/{id}/image", api_vault_image)
r.add_put("/api/vault/{id}", api_vault_update)
r.add_get("/api/audience-counts", api_audience_counts)
r.add_post("/api/broadcast", api_broadcast)
r.add_get("/api/export/leads.csv", api_export_leads)


# ==============================================================================
# RUNNER — web CRM + Telegram bot in one process
# ==============================================================================
BOT_POLLING = False    # True once this copy is the one receiving Telegram messages
BOT_LOCK_ID = 246246   # any fixed number; shared by every copy of this bot
_bot_lock_conn = None


async def acquire_bot_lock():
    """Only ONE copy of the bot may poll Telegram. With Postgres, copies race for an advisory
    lock; the losers keep serving the CRM and retry every 30 seconds (e.g. during a deploy)."""
    global _bot_lock_conn
    if not USE_PG:
        return
    import asyncpg
    while True:
        try:
            conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
            if await conn.fetchval("SELECT pg_try_advisory_lock($1)", BOT_LOCK_ID):
                _bot_lock_conn = conn
                logging.info("Bot lock acquired: this copy runs the Telegram bot.")
                asyncio.create_task(_watch_bot_lock())
                return
            await conn.close()
            logging.warning("Another copy of the bot is running. Serving the CRM only; retrying in 30s.")
        except Exception as e:
            logging.warning(f"Bot lock check failed ({e}); retrying in 30s.")
        await asyncio.sleep(30)


async def _watch_bot_lock():
    """If the lock connection drops, another copy could take over: stop this one so Render restarts it."""
    while True:
        await asyncio.sleep(60)
        try:
            await _bot_lock_conn.fetchval("SELECT 1")
        except Exception as e:
            logging.error(f"Lost the bot lock connection ({e}). Restarting to avoid two bots polling.")
            os._exit(1)


async def main():
    await init_db()

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    print("=" * 60)
    print(f"  DATABASE: {'Postgres (DATABASE_URL)' if USE_PG else 'SQLite ' + DB_PATH}")
    print(f"  PLAY 246 CRM IS LIVE AT: http://localhost:{WEB_PORT}")
    if not CRM_PASSWORD:
        print("  ⚠️  CRM_PASSWORD is not set — the CRM is open to anyone with the link.")
    print("=" * 60)

    if not RUN_BOT:
        print("  RUN_BOT=0 → Telegram polling disabled (web only).")
        await asyncio.Event().wait()
        return

    await acquire_bot_lock()
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Start the bot")],
                                  scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands([
            BotCommand(command="menu", description="Open the admin menu"),
            BotCommand(command="r", description="Send a saved reply (inside a customer topic)"),
            BotCommand(command="broadcast", description="Send a broadcast"),
            BotCommand(command="stats", description="Live stats"),
            BotCommand(command="export_leads", description="Download leads CSV"),
            BotCommand(command="cancel", description="Cancel what you're doing"),
            BotCommand(command="help", description="Help"),
        ], scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logging.warning(f"Could not set bot commands: {e}")

    global BOT_POLLING
    BOT_POLLING = True
    asyncio.create_task(quiet_loop())
    print("  TELEGRAM BOT IS POLLING")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
