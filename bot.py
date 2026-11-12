import asyncio
import logging
import aiosqlite
import os
import sys
import json
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats
)

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ==============================================================================
# CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9011031953:AAEBkNzzKEwc8gDeYSyidt0ff97cFo9aEkc")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1004349789648"))
WEB_PORT = int(os.getenv("PORT", 8080))

DB_PATH = os.path.join(os.path.dirname(__file__), "leads.db")
CRM_HTML_PATH = os.path.join(os.path.dirname(__file__), "crm.html")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ==============================================================================
# DATABASE INITIALIZATION
# ==============================================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                phone_number TEXT,
                topic_id INTEGER,
                is_pending INTEGER DEFAULT 0,
                last_message TEXT,
                last_time TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sender TEXT,
                text TEXT,
                media_type TEXT,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS flow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_order INTEGER,
                title TEXT,
                trigger_type TEXT,
                content TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sender_name TEXT,
                phone_number TEXT,
                file_id TEXT,
                utr_code TEXT,
                timestamp TEXT
            )
        """)

        # Run column migrations for existing tables
        for col, col_def in [
            ("is_pending", "INTEGER DEFAULT 0"),
            ("last_message", "TEXT"),
            ("last_time", "TEXT")
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
            except Exception:
                pass # Already exists

        # Populate default flow steps if table is empty
        async with db.execute("SELECT COUNT(*) FROM flow_steps") as c:
            count = (await c.fetchone())[0]

        if count == 0:
            await db.execute("""
                INSERT INTO flow_steps (step_order, title, trigger_type, content) VALUES
                (1, 'Welcome & Contact Request', '/start', '👋 Welcome {name} to Play 246 Auto ID!\n\n⬇️ OFFER LENE KE LIYE ⬇️\n👉 Neeche 📱 SHARE MOBILE NUMBER button dabayein.\n❌ Number type na karein.'),
                (2, 'Site Links & Demo Info', 'contact_shared', '✅ Aapka Offer Activate Ho Gaya Hai!\n\n🟠 SITE 1 – (Auto Site): https://play246.co/?code=Play246\n🟢 SITE 2 – (Lotus/Ultra): https://play246.fun/home\n🔵 SITE 3 – (Diamond): https://portal246.com/home\n\nMin Deposit : ₹500 | Demo ID Available'),
                (3, 'VIP Follow-up Reminder', 'inactivity_15m', 'Need help creating your Demo ID? Reply here or tap our VIP WhatsApp agent: https://walink.online/Play246')
            """)

        await db.commit()


# ==============================================================================
# HELPER DB FUNCTIONS
# ==============================================================================
async def log_message(user_id: int, sender: str, text: str, media_type: str = "text"):
    now_str = datetime.now().strftime("%I:%M %p")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, sender, text, media_type, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, sender, text, media_type, now_str)
        )
        is_pending = 1 if sender == "customer" else 0
        await db.execute(
            "UPDATE users SET last_message = ?, last_time = ?, is_pending = ? WHERE user_id = ?",
            (text[:100], now_str, is_pending, user_id)
        )
        await db.commit()

async def get_or_create_topic(user: types.User, phone: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT topic_id FROM users WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
            topic_id = row[0] if row else None

    if not topic_id:
        phone_label = f" (+{phone.lstrip('+')})" if phone else ""
        topic_title = f"{user.first_name}{phone_label}"[:128]
        try:
            topic = await bot.create_forum_topic(chat_id=ADMIN_GROUP_ID, name=topic_title)
            topic_id = topic.message_thread_id
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO users (user_id, first_name, username, phone_number, topic_id, is_pending, last_time)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        first_name = excluded.first_name,
                        username = excluded.username,
                        phone_number = COALESCE(excluded.phone_number, users.phone_number),
                        topic_id = excluded.topic_id
                """, (user.id, user.first_name, user.username or "", phone, topic_id, datetime.now().strftime("%I:%M %p")))
                await db.commit()
        except Exception as e:
            logging.error(f"Failed to create forum topic: {e}")
            return None

    return topic_id


# ==============================================================================
# TELEGRAM BOT FLOW
# ==============================================================================
@dp.message(CommandStart(), F.chat.type == "private")
async def start_handler(message: types.Message):
    user = message.from_user
    now_str = datetime.now().strftime("%I:%M %p")
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, first_name, username, last_time)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                last_time = excluded.last_time
        """, (user.id, user.first_name or "Player", user.username or "", now_str))
        
        # Fetch Step 1 template
        async with db.execute("SELECT content FROM flow_steps WHERE step_order = 1") as c:
            row = await c.fetchone()
            template = row[0] if row else "Welcome {name}!"
        await db.commit()

    contact_btn = KeyboardButton(text="📱 Share Mobile Number", request_contact=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[contact_btn]], resize_keyboard=True, one_time_keyboard=True)

    welcome_text = template.replace("{name}", user.first_name or "Player")
    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")
    await log_message(user.id, "bot", welcome_text)

@dp.message(F.contact, F.chat.type == "private")
async def contact_handler(message: types.Message):
    user = message.from_user
    phone = message.contact.phone_number

    topic_id = await get_or_create_topic(user, phone=phone)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET phone_number = ? WHERE user_id = ?", (phone, user.id))
        async with db.execute("SELECT content FROM flow_steps WHERE step_order = 2") as c:
            row = await c.fetchone()
            offers_text = row[0] if row else "Welcome to Play 246!"
        await db.commit()

    # Post contact card inside Admin Topic
    if topic_id:
        try:
            await bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                message_thread_id=topic_id,
                text=f"🔥 **NEW VERIFIED LEAD**\n👤 **Name:** {user.first_name}\n📞 **Phone:** `+{phone.lstrip('+')}`\n🆔 **ID:** `{user.id}`",
                parse_mode="Markdown"
            )
            card = await bot.send_contact(
                chat_id=ADMIN_GROUP_ID,
                message_thread_id=topic_id,
                phone_number=phone,
                first_name=user.first_name,
                last_name=user.last_name or ""
            )
            # Pin the contact card to the header!
            await bot.pin_chat_message(chat_id=ADMIN_GROUP_ID, message_id=card.message_id)
        except Exception as e:
            logging.error(f"Error posting topic lead: {e}")

    await message.answer(offers_text, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    await log_message(user.id, "customer", f"📱 Shared Contact: +{phone.lstrip('+')}")
    await log_message(user.id, "bot", offers_text)

@dp.message(F.photo, F.chat.type == "private")
async def photo_handler(message: types.Message):
    user = message.from_user
    photo = message.photo[-1]
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT phone_number FROM users WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
            phone = row[0] if row else "Unknown"

        # Save to Media Vault
        await db.execute("""
            INSERT INTO media_vault (user_id, sender_name, phone_number, file_id, utr_code, timestamp)
            VALUES (?, ?, ?, ?, 'Pending Verification', ?)
        """, (user.id, user.first_name, phone, photo.file_id, datetime.now().strftime("%I:%M %p")))
        await db.commit()

    topic_id = await get_or_create_topic(user, phone=phone)
    if topic_id:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)

    await log_message(user.id, "customer", "🖼️ Sent Payment Screenshot", media_type="photo")

@dp.message(F.chat.type == "private")
async def customer_message_relay(message: types.Message):
    user = message.from_user
    msg_text = message.text or message.caption or "[Media message]"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT phone_number FROM users WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
            phone = row[0] if row else None

    topic_id = await get_or_create_topic(user, phone=phone)
    if topic_id:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)

    await log_message(user.id, "customer", msg_text)

@dp.message(Command("view_templates"), F.chat.id == ADMIN_GROUP_ID)
async def view_templates_cmd(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, step_order, title, trigger_type, content FROM flow_steps ORDER BY step_order ASC") as c:
            rows = await c.fetchall()

    if not rows:
        await message.reply("No flow steps configured yet.")
        return

    for r in rows:
        step_id, order, title, trigger, content = r
        text = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Step {order} — {title}\n"
            f"ID: {step_id}  |  Trigger: {trigger}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{content}\n\n"
            f"Reply /edit_step {step_id} <new text> to update"
        )
        await message.answer(text, message_thread_id=message.message_thread_id)
        await asyncio.sleep(0.3)


@dp.message(Command("set_welcome"), F.chat.id == ADMIN_GROUP_ID)
async def set_welcome_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /set_welcome <new welcome message text>\n\nUse {name} as a placeholder for the user's name.")
        return
    new_text = parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE flow_steps SET content = ? WHERE step_order = 1", (new_text,))
        await db.commit()
    await message.reply(f"Welcome message (Step 1) updated.\n\nNew text:\n{new_text}")


@dp.message(Command("set_links"), F.chat.id == ADMIN_GROUP_ID)
async def set_links_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /set_links <new site links message text>")
        return
    new_text = parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE flow_steps SET content = ? WHERE step_order = 2", (new_text,))
        await db.commit()
    await message.reply(f"Site links message (Step 2) updated.\n\nNew text:\n{new_text}")


@dp.message(Command("add_step"), F.chat.id == ADMIN_GROUP_ID)
async def add_step_cmd(message: types.Message):
    """Usage: /add_step Title | trigger_type | Message content"""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        await message.reply(
            "Usage: /add_step Title | trigger_type | Message content\n\n"
            "Triggers: /start, contact_shared, inactivity_15m, custom, manual"
        )
        return
    segments = [s.strip() for s in parts[1].split("|", 2)]
    if len(segments) < 3:
        await message.reply("Need 3 parts separated by |  (title | trigger | content)")
        return
    title, trigger, content = segments
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT MAX(step_order) FROM flow_steps") as c:
            max_order = (await c.fetchone())[0] or 0
        await db.execute(
            "INSERT INTO flow_steps (step_order, title, trigger_type, content) VALUES (?, ?, ?, ?)",
            (max_order + 1, title, trigger, content)
        )
        await db.commit()
    await message.reply(f"New step added as Step {max_order + 1}: {title}\nTrigger: {trigger}")


@dp.message(Command("edit_step"), F.chat.id == ADMIN_GROUP_ID)
async def edit_step_cmd(message: types.Message):
    """Usage: /edit_step <id> <new content>"""
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /edit_step <step_id> <new message text>")
        return
    try:
        step_id = int(parts[1])
    except ValueError:
        await message.reply("Step ID must be a number.")
        return
    new_content = parts[2]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM flow_steps WHERE id = ?", (step_id,)) as c:
            if not await c.fetchone():
                await message.reply(f"Step {step_id} not found. Use /view_templates to see all steps.")
                return
        await db.execute("UPDATE flow_steps SET content = ? WHERE id = ?", (new_content, step_id))
        await db.commit()
    await message.reply(f"Step {step_id} content updated.\n\nNew text:\n{new_content}")


@dp.message(Command("delete_step"), F.chat.id == ADMIN_GROUP_ID)
async def delete_step_cmd(message: types.Message):
    """Usage: /delete_step <id>"""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /delete_step <step_id>")
        return
    try:
        step_id = int(parts[1])
    except ValueError:
        await message.reply("Step ID must be a number.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT title FROM flow_steps WHERE id = ?", (step_id,)) as c:
            row = await c.fetchone()
            if not row:
                await message.reply(f"Step {step_id} not found.")
                return
            title = row[0]
        await db.execute("DELETE FROM flow_steps WHERE id = ?", (step_id,))
        await db.commit()
    await message.reply(f"Step {step_id} ({title}) has been deleted.")


@dp.message(Command("set_trigger"), F.chat.id == ADMIN_GROUP_ID)
async def set_trigger_cmd(message: types.Message):
    """Usage: /set_trigger <id> <trigger_type>"""
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "Usage: /set_trigger <step_id> <trigger_type>\n\n"
            "Available triggers: /start, contact_shared, inactivity_15m, custom, manual"
        )
        return
    try:
        step_id = int(parts[1])
    except ValueError:
        await message.reply("Step ID must be a number.")
        return
    trigger = parts[2]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM flow_steps WHERE id = ?", (step_id,)) as c:
            if not await c.fetchone():
                await message.reply(f"Step {step_id} not found.")
                return
        await db.execute("UPDATE flow_steps SET trigger_type = ? WHERE id = ?", (trigger, step_id))
        await db.commit()
    await message.reply(f"Step {step_id} trigger updated to: {trigger}")


@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def stats_cmd(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE phone_number IS NOT NULL") as c:
            verified = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_pending = 1") as c:
            pending = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM media_vault") as c:
            screenshots = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE phone_number IS NOT NULL AND user_id NOT IN (SELECT DISTINCT user_id FROM media_vault)") as c:
            unpaid = (await c.fetchone())[0]

    text = (
        f"--- PLAY 246 LIVE STATS ---\n\n"
        f"Total Leads:       {total}\n"
        f"Verified (Phone):  {verified}\n"
        f"Pending Reply:     {pending}\n"
        f"Screenshots Sent:  {screenshots}\n"
        f"Unpaid (No SS):    {unpaid}\n\n"
        f"CRM Dashboard: https://play246-crm.onrender.com"
    )
    await message.reply(text)


@dp.message(Command("broadcast"), F.chat.id == ADMIN_GROUP_ID)
async def broadcast_cmd(message: types.Message):
    """Show inline keyboard to pick audience, then wait for next message."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="All Users", callback_data="bc_all"),
            InlineKeyboardButton(text="Verified", callback_data="bc_verified")
        ],
        [
            InlineKeyboardButton(text="Pending", callback_data="bc_pending"),
            InlineKeyboardButton(text="Unpaid (No SS)", callback_data="bc_unpaid")
        ]
    ])
    await message.reply(
        "Select Audience for Broadcast:\n\nAfter selecting, send your broadcast message (text, photo, or sticker) as a reply to the audience confirmation.",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("bc_"))
async def broadcast_audience_callback(callback: CallbackQuery):
    audience_map = {
        "bc_all": ("all", "All Users"),
        "bc_verified": ("verified", "Verified Leads"),
        "bc_pending": ("pending", "Pending / Unverified"),
        "bc_unpaid": ("unpaid", "Unpaid (No Screenshot)")
    }
    audience_key, audience_label = audience_map.get(callback.data, ("all", "All Users"))

    await callback.message.edit_text(
        f"Audience: {audience_label}\n\n"
        f"Now send your broadcast message (text, photo, sticker, or any media) as a REPLY to this message.",
        reply_markup=None
    )
    # Store the pending broadcast audience in a simple in-memory dict keyed by message_id
    pending_broadcasts[callback.message.message_id] = audience_key
    await callback.answer(f"Audience set to: {audience_label}")


@dp.message(Command("export_leads"), F.chat.id == ADMIN_GROUP_ID)
async def export_leads_cmd(message: types.Message):
    import io
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, first_name, username, phone_number, last_time FROM users ORDER BY user_id") as c:
            rows = await c.fetchall()

    if not rows:
        await message.reply("No leads found.")
        return

    csv_buf = io.StringIO()
    csv_buf.write("user_id,first_name,username,phone_number,last_seen\n")
    for r in rows:
        csv_buf.write(f"{r[0]},{r[1]},{r[2]},{r[3] or ''},{r[4] or ''}\n")

    csv_bytes = csv_buf.getvalue().encode("utf-8")
    file = BufferedInputFile(csv_bytes, filename=f"Play246_Leads_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
    await message.reply_document(file, caption=f"Exported {len(rows)} leads.")


@dp.message(Command("help"), F.chat.id == ADMIN_GROUP_ID)
async def help_cmd(message: types.Message):
    text = (
        "--- PLAY 246 ADMIN COMMANDS ---\n\n"
        "/view_templates — View all bot flow steps\n"
        "/set_welcome <text> — Edit welcome message (Step 1)\n"
        "/set_links <text> — Edit site links message (Step 2)\n"
        "/add_step Title | trigger | content — Add new step\n"
        "/edit_step <id> <text> — Edit a step's content\n"
        "/delete_step <id> — Remove a step\n"
        "/set_trigger <id> <trigger> — Change a step's trigger\n"
        "/broadcast — Send broadcast with audience picker\n"
        "/export_leads — Download leads as CSV\n"
        "/stats — View live dashboard stats\n"
        "/help — Show this help menu\n\n"
        "CRM Dashboard: https://play246-crm.onrender.com"
    )
    await message.reply(text)


# In-memory store for pending broadcast audience selection
pending_broadcasts: dict[int, str] = {}

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.reply_to_message)
async def handle_broadcast_reply(message: types.Message):
    """If replying to a broadcast audience selection message, send as broadcast."""
    if not message.reply_to_message:
        return
    reply_to_id = message.reply_to_message.message_id
    if reply_to_id not in pending_broadcasts:
        # Not a broadcast reply — fall through to normal admin relay
        return await _admin_relay(message)

    audience = pending_broadcasts.pop(reply_to_id)

    async with aiosqlite.connect(DB_PATH) as db:
        if audience == "verified":
            sql = "SELECT user_id, topic_id FROM users WHERE phone_number IS NOT NULL"
        elif audience == "pending":
            sql = "SELECT user_id, topic_id FROM users WHERE phone_number IS NULL"
        elif audience == "unpaid":
            sql = "SELECT user_id, topic_id FROM users WHERE phone_number IS NOT NULL AND user_id NOT IN (SELECT DISTINCT user_id FROM media_vault)"
        else:
            sql = "SELECT user_id, topic_id FROM users"
        async with db.execute(sql) as c:
            users = await c.fetchall()

    delivered = 0
    failed = 0
    for uid, _ in users:
        try:
            await message.copy_to(chat_id=uid)
            delivered += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1

    audience_labels = {"all": "All", "verified": "Verified", "pending": "Pending", "unpaid": "Unpaid"}
    await message.reply(
        f"Broadcast Finished!\n\n"
        f"Target Audience: {audience_labels.get(audience, audience)}\n"
        f"Delivered: {delivered}\n"
        f"Failed / Blocked: {failed}"
    )


@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply_relay(message: types.Message):
    await _admin_relay(message)


async def _admin_relay(message: types.Message):
    """Core relay logic — forward admin topic replies to the customer."""
    if message.forum_topic_created or message.is_automatic_forward:
        return

    # Ignore bot commands in the general topic
    if message.text and message.text.startswith("/"):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE topic_id = ?", (message.message_thread_id,)) as cursor:
            row = await cursor.fetchone()
            customer_user_id = row[0] if row else None

    if not customer_user_id:
        return

    try:
        await message.copy_to(chat_id=customer_user_id)
        msg_text = message.text or message.caption or "[Admin Voice/Media]"
        await log_message(customer_user_id, "agent", msg_text)
    except Exception as e:
        await message.reply(f"Delivery failed: {e}")


# ==============================================================================
# CRM REST API (SERVED ON PORT 8080)
# ==============================================================================
app = web.Application()

async def serve_index(request):
    if os.path.exists(CRM_HTML_PATH):
        with open(CRM_HTML_PATH, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    return web.Response(text="CRM HTML not found", status=404)

async def api_stats(request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_leads = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE phone_number IS NOT NULL") as c:
            verified_leads = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_pending = 1") as c:
            pending_replies = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages") as c:
            total_messages = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM media_vault") as c:
            vault_count = (await c.fetchone())[0]

    return web.json_response({
        "total_leads": total_leads,
        "verified_leads": verified_leads,
        "pending_replies": pending_replies,
        "total_messages": total_messages,
        "vault_count": vault_count
    })

async def api_chats(request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, first_name, username, phone_number, is_pending, last_message, last_time FROM users ORDER BY is_pending DESC, user_id DESC") as cursor:
            rows = await cursor.fetchall()
    
    chats = [{
        "user_id": r[0],
        "first_name": r[1] or "Player",
        "username": r[2] or "",
        "phone_number": r[3] or "",
        "is_pending": r[4],
        "last_message": r[5] or "Started conversation",
        "last_time": r[6] or ""
    } for r in rows]
    return web.json_response(chats)

async def api_chat_messages(request):
    user_id = int(request.match_info["user_id"])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT sender, text, timestamp FROM messages WHERE user_id = ? ORDER BY id ASC", (user_id,)) as cursor:
            rows = await cursor.fetchall()

    messages = [{"sender": r[0], "text": r[1], "timestamp": r[2]} for r in rows]
    return web.json_response(messages)

async def api_reply_customer(request):
    user_id = int(request.match_info["user_id"])
    file_bytes = None
    file_name = None

    if request.content_type.startswith("multipart/"):
        reader = await request.post()
        text = reader.get("text", "").strip()
        file_field = reader.get("file")
        if file_field and hasattr(file_field, "file"):
            file_bytes = file_field.file.read()
            file_name = file_field.filename
    else:
        data = await request.json()
        text = data.get("text", "").strip()

    if not text and not file_bytes:
        return web.json_response({"error": "Empty message and no file attached"}, status=400)

    # Fetch topic_id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT topic_id FROM users WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
            topic_id = row[0] if row else None

    # Send directly to customer on Telegram
    if file_bytes:
        photo_file = BufferedInputFile(file_bytes, filename=file_name or "reply.jpg")
        await bot.send_photo(chat_id=user_id, photo=photo_file, caption=text or None)
        if topic_id:
            try:
                topic_photo = BufferedInputFile(file_bytes, filename=file_name or "reply.jpg")
                await bot.send_photo(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id, photo=topic_photo, caption=f"👤 **[CRM Web Reply]**\n{text}")
            except Exception:
                pass
        await log_message(user_id, "agent", text or "🖼️ Sent Photo Attachment", media_type="photo")
    else:
        await bot.send_message(chat_id=user_id, text=text)
        if topic_id:
            try:
                await bot.send_message(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id, text=f"👤 **[CRM Web Reply]**\n{text}")
            except Exception:
                pass
        await log_message(user_id, "agent", text)

    return web.json_response({"status": "sent"})

async def api_get_steps(request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, step_order, title, trigger_type, content FROM flow_steps ORDER BY step_order ASC") as c:
            rows = await c.fetchall()

    steps = [{"id": r[0], "step_order": r[1], "title": r[2], "trigger_type": r[3], "content": r[4]} for r in rows]
    return web.json_response(steps)

async def api_add_step(request):
    data = await request.json()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT MAX(step_order) FROM flow_steps") as c:
            max_order = (await c.fetchone())[0] or 0
        await db.execute(
            "INSERT INTO flow_steps (step_order, title, trigger_type, content) VALUES (?, ?, ?, ?)",
            (max_order + 1, data.get("title", "New Step"), data.get("trigger_type", "custom"), data.get("content", ""))
        )
        await db.commit()
    return web.json_response({"status": "created"})

async def api_update_step(request):
    step_id = int(request.match_info["id"])
    data = await request.json()
    content = data.get("content", "")
    trigger_type = data.get("trigger_type", "/start")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE flow_steps SET content = ?, trigger_type = ? WHERE id = ?", (content, trigger_type, step_id))
        await db.commit()
    return web.json_response({"status": "updated"})

async def api_delete_step(request):
    step_id = int(request.match_info["id"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM flow_steps WHERE id = ?", (step_id,))
        await db.commit()
    return web.json_response({"status": "deleted"})

async def api_get_vault(request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT sender_name, phone_number, utr_code, timestamp FROM media_vault ORDER BY id DESC") as c:
            rows = await c.fetchall()
    vault = [{"sender_name": r[0], "phone_number": r[1], "utr_code": r[2], "timestamp": r[3]} for r in rows]
    return web.json_response(vault)

async def api_broadcast(request):
    file_bytes = None
    file_name = None
    
    if request.content_type.startswith("multipart/"):
        reader = await request.post()
        audience = reader.get("audience", "all")
        text = reader.get("text", "").strip()
        file_field = reader.get("file")
        if file_field and hasattr(file_field, "file"):
            file_bytes = file_field.file.read()
            file_name = file_field.filename
    else:
        data = await request.json()
        audience = data.get("audience", "all")
        text = data.get("text", "").strip()

    if not text and not file_bytes:
        return web.json_response({"error": "Empty message and no file attached"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        if audience == "verified":
            sql = "SELECT user_id, topic_id FROM users WHERE phone_number IS NOT NULL"
        elif audience == "pending":
            sql = "SELECT user_id, topic_id FROM users WHERE phone_number IS NULL"
        elif audience == "unpaid":
            sql = "SELECT user_id, topic_id FROM users WHERE phone_number IS NOT NULL AND user_id NOT IN (SELECT DISTINCT user_id FROM media_vault)"
        else:
            sql = "SELECT user_id, topic_id FROM users"

        async with db.execute(sql) as c:
            users = await c.fetchall()

    delivered = 0
    for uid, user_topic_id in users:
        try:
            if file_bytes:
                # Send photo / poster with optional caption
                photo_file = BufferedInputFile(file_bytes, filename=file_name or "poster.jpg")
                await bot.send_photo(chat_id=uid, photo=photo_file, caption=text or None)
                if user_topic_id:
                    try:
                        topic_photo = BufferedInputFile(file_bytes, filename=file_name or "poster.jpg")
                        await bot.send_photo(chat_id=ADMIN_GROUP_ID, message_thread_id=user_topic_id, photo=topic_photo, caption=f"📢 **[Broadcast Sent]**\n{text}")
                    except Exception:
                        pass
            else:
                await bot.send_message(chat_id=uid, text=text)
                if user_topic_id:
                    try:
                        await bot.send_message(chat_id=ADMIN_GROUP_ID, message_thread_id=user_topic_id, text=f"📢 **[Broadcast Sent]**\n{text}")
                    except Exception:
                        pass
            delivered += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass

    return web.json_response({"delivered": delivered})


# Health check endpoint for uptime monitors (UptimeRobot, etc.)
async def health_check(request):
    return web.json_response({"status": "alive", "bot": "Play 246 Support Bot", "uptime": "ok"})

# Configure Web Routes
app.router.add_get("/health", health_check)
app.router.add_get("/", serve_index)
app.router.add_get("/api/stats", api_stats)
app.router.add_get("/api/chats", api_chats)
app.router.add_get("/api/chats/{user_id}/messages", api_chat_messages)
app.router.add_post("/api/chats/{user_id}/reply", api_reply_customer)
app.router.add_get("/api/steps", api_get_steps)
app.router.add_post("/api/steps", api_add_step)
app.router.add_put("/api/steps/{id}", api_update_step)
app.router.add_delete("/api/steps/{id}", api_delete_step)
app.router.add_get("/api/vault", api_get_vault)
app.router.add_post("/api/broadcast", api_broadcast)


# ==============================================================================
# UNIFIED RUNNER (RUNS WEB CRM + TELEGRAM BOT SIMULTANEOUSLY)
# ==============================================================================
async def main():
    await init_db()

    # Register bot command menus
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Start the bot"),
        ], scope=BotCommandScopeAllPrivateChats())

        await bot.set_my_commands([
            BotCommand(command="view_templates", description="View all bot flow steps"),
            BotCommand(command="set_welcome", description="Edit welcome message (Step 1)"),
            BotCommand(command="set_links", description="Edit site links (Step 2)"),
            BotCommand(command="add_step", description="Add new flow step"),
            BotCommand(command="edit_step", description="Edit a step's content"),
            BotCommand(command="delete_step", description="Delete a flow step"),
            BotCommand(command="set_trigger", description="Change step trigger type"),
            BotCommand(command="broadcast", description="Send broadcast message"),
            BotCommand(command="export_leads", description="Download leads CSV"),
            BotCommand(command="stats", description="Live dashboard stats"),
            BotCommand(command="help", description="Show all admin commands"),
        ], scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logging.warning(f"Could not set bot commands: {e}")

    # Start Aiohttp Web Server for CRM
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print("=" * 60)
    print(f"  PLAY 246 CRM IS LIVE AT: http://localhost:{WEB_PORT}")
    print(f"  TELEGRAM BOT IS POLLING SUCCESSFULLY!")
    print("=" * 60)

    # Start Telegram Bot Polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
