"""End-to-end tests with a fake Telegram session (no network)."""
import asyncio, os, sys, shutil, json, tempfile
from datetime import datetime, timezone, timedelta

WORK = sys.argv[1]
TMP = tempfile.mkdtemp()
shutil.copy(os.path.join(WORK, "leads.db"), os.path.join(TMP, "leads.db"))
os.environ["DB_PATH"] = os.path.join(TMP, "leads.db")
os.environ["CRM_PASSWORD"] = "secret123"
sys.path.insert(0, WORK)

import bot as B
from aiogram.client.session.base import BaseSession
from aiogram import types as T
from aiogram.methods import CopyMessage, CreateForumTopic
from aiohttp.test_utils import TestClient, TestServer

GROUP = B.ADMIN_GROUP_ID
SENT = []
BOT_NAME = {"name": "Prototype Bot"}
_id = [1000]


def nid():
    _id[0] += 1
    return _id[0]


class FakeSession(BaseSession):
    async def close(self): pass
    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        d = method.model_dump(exclude_none=True)
        SENT.append((name, d))
        chat_id = d.get("chat_id", 1)
        chat = T.Chat(id=chat_id, type="private" if isinstance(chat_id, int) and chat_id > 0 else "supergroup")
        now = datetime.now()
        if isinstance(method, CreateForumTopic):
            return T.ForumTopic(message_thread_id=nid(), name=d["name"], icon_color=0)
        if isinstance(method, CopyMessage):
            return T.MessageId(message_id=nid())
        if name in ("PinChatMessage", "AnswerCallbackQuery", "SetMyCommands"):
            return True
        if name == "SetMyName":
            if BOT_NAME.get("rate_limited"):
                from aiogram.exceptions import TelegramRetryAfter
                raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=300)
            BOT_NAME["name"] = d.get("name", "")
            return True
        if name == "GetMyName":
            return T.BotName(name=BOT_NAME["name"])
        extra = {}
        if name == "SendPhoto":
            extra["photo"] = [T.PhotoSize(file_id="tgphoto123", file_unique_id="u", width=1, height=1)]
        return T.Message(message_id=nid(), date=now, chat=chat, text=d.get("text"), caption=d.get("caption"), **extra)


B.bot.session = FakeSession()
CUST = T.User(id=555, is_bot=False, first_name="Rahul")
ADMIN = T.User(id=777, is_bot=False, first_name="Ansh")
PCHAT = T.Chat(id=555, type="private")
GCHAT = T.Chat(id=GROUP, type="supergroup", is_forum=True)


def upd(**msg):
    base = dict(message_id=nid(), date=datetime.now())
    base.update(msg)
    return T.Update(update_id=nid(), message=T.Message(**base))


async def feed(u):
    await B.dp.feed_update(B.bot, u)


def last(name, **match):
    for n, d in reversed(SENT):
        if n == name and all(d.get(k) == v for k, v in match.items()):
            return d
    return None


def texts_to(chat_id):
    return [d.get("text") or d.get("caption") for n, d in SENT if d.get("chat_id") == chat_id and n.startswith("Send")]


ok = 0
def check(cond, label):
    global ok
    if not cond:
        print("FAIL:", label)
        raise SystemExit(1)
    ok += 1
    print("  ✓", label)


async def callback(data, msg_id=None, thread=None):
    m = T.Message(message_id=msg_id or nid(), date=datetime.now(), chat=GCHAT,
                  from_user=T.User(id=B.bot.id if False else 999, is_bot=True, first_name="bot"),
                  text="menu", message_thread_id=thread, is_topic_message=bool(thread))
    await feed(T.Update(update_id=nid(), callback_query=T.CallbackQuery(
        id=str(nid()), from_user=ADMIN, chat_instance="x", data=data, message=m)))


async def main():
    await B.init_db()
    msgs = await B.list_messages()
    kinds = [m["kind"] for m in msgs]
    check(kinds.count("start") == 1 and kinds.count("fallback") == 1, "seeded core messages")
    start = await B.get_core("start")
    check("Play 246 Auto ID" in start["text"], "welcome text migrated from old flow_steps")

    # 1) customer starts
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, text="/start", entities=[T.MessageEntity(type="bot_command", offset=0, length=6)]))
    d = last("SendMessage", chat_id=555)
    check(d and "Welcome Rahul" in d["text"], "welcome sent with customer name")
    check("keyboard" in d["reply_markup"], "share-number button attached")

    # 2) shares contact
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, contact=T.Contact(phone_number="919876543210", first_name="Rahul", user_id=555)))
    check(last("CreateForumTopic") is not None, "topic created for the customer")
    d = last("SendMessage", chat_id=555)
    check(d and "Offer Activate" in d["text"], "site links sent after contact")
    u = await B.db_one("SELECT * FROM users WHERE user_id=555")
    topic = u["topic_id"]
    check(u["phone_number"] == "919876543210", "phone saved")

    # 3) keyword questions
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, text="Bhai deposit kaise kare??"))
    d = last("SendMessage", chat_id=555)
    check(d and "Deposit karna" in d["text"], "keyword 'deposit' → Deposit help")
    check(any("Auto-reply sent · Deposit help" in (x.get("text") or "") for n, x in SENT if x.get("message_thread_id") == topic),
          "team sees auto-reply note in the topic")
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, text="mera withdrawal kab aayega"))
    check("Withdrawal" in (last("SendMessage", chat_id=555) or {}).get("text", ""), "prefix match: withdrawal → Withdrawal help")

    # 4) fallback + alert + no repeat
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, text="hello kya haal"))
    check("message mil gaya" in (last("SendMessage", chat_id=555) or {}).get("text", ""), "unknown message → didn't-understand reply")
    alert = last("SendMessage", chat_id=GROUP, message_thread_id=topic)
    check(alert and "couldn't answer" in alert["text"] and "inline_keyboard" in alert["reply_markup"], "team alerted with saved-reply buttons")
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, text="aur kuch?"))
    check(last("SendMessage", chat_id=555) is None, "didn't-understand reply not repeated within 30 min")

    # 5) team replies → bot stays quiet
    SENT.clear()
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="Haan bolo bhai", message_thread_id=topic, is_topic_message=True))
    check(last("CopyMessage", chat_id=555) is not None, "team reply in topic relayed to customer")
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, text="deposit"))
    check(last("SendMessage", chat_id=555) is None, "bot stays quiet while team is talking (handoff)")
    sim = await B.decide_reply("text", "deposit", user_id=555)
    check("stays quiet" in sim["reason"], "handoff reason is in plain English")
    await B.db_run("UPDATE users SET last_agent_at = NULL WHERE user_id=555")

    # 6) photo
    SENT.clear()
    await feed(upd(chat=PCHAT, from_user=CUST, photo=[T.PhotoSize(file_id="ss1", file_unique_id="a", width=1, height=1)]))
    check((await B.db_one("SELECT COUNT(*) n FROM media_vault"))["n"] == 1, "screenshot saved to vault")
    check("Screenshot mil gaya" in (last("SendMessage", chat_id=555) or {}).get("text", ""), "screenshot acknowledgement sent")

    # 7) admin menu wizard: add keyword auto-reply
    SENT.clear()
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="/menu", entities=[T.MessageEntity(type="bot_command", offset=0, length=5)]))
    check(last("SendMessage", chat_id=GROUP) and "Admin Menu" in last("SendMessage", chat_id=GROUP)["text"], "/menu shows buttons")
    await callback("am:new")
    await callback("am:newk:keywords")
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="kyc, documents"))
    check("Now send the reply" in (last("SendMessage", chat_id=GROUP) or {}).get("text", ""), "wizard asks for reply after words")
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="📄 KYC ke liye Aadhaar bhejein [name] ji"))
    prev = [d for n, d in SENT if d.get("chat_id") == GROUP and "Aadhaar" in (d.get("text") or "")]
    check(prev and "Rahul ji" in prev[-1]["text"], "wizard shows preview with sample name")
    await callback("am:save")
    new = await B.db_one("SELECT * FROM bot_messages WHERE keywords LIKE '%kyc%'")
    check(new and new["is_active"] == 1, "new auto-reply saved from Telegram")
    sim = await B.decide_reply("text", "KYC kaise karu", simulate=True)
    check(sim["message"]["id"] == new["id"], "new auto-reply is used by the engine immediately")

    # wizard input in a topic is NOT relayed to customer
    await callback("am:test")
    SENT.clear()
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="bonus kab milega", message_thread_id=topic, is_topic_message=True))
    check(last("CopyMessage", chat_id=555) is None, "test input typed in a topic is not sent to the customer")
    check(any("Bonus" in (d.get("text") or "") for n, d in SENT), "Telegram test shows which answer fires")

    # edit text via menu
    await callback(f"am:text:{start['id']}")
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="Namaste [name]! Number share karein 👇"))
    await callback("am:save")
    check((await B.get_core("start"))["text"].startswith("Namaste"), "welcome edited from Telegram")

    # /r inside topic
    SENT.clear()
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="/r", message_thread_id=topic, is_topic_message=True,
                   entities=[T.MessageEntity(type="bot_command", offset=0, length=2)]))
    d = last("SendMessage", chat_id=GROUP)
    check(d and "inline_keyboard" in d["reply_markup"], "/r shows saved replies in topic")
    saved = (await B.list_messages("saved"))[0]
    SENT.clear()
    await callback(f"sr:{saved['id']}:555", thread=topic)
    check(last("SendMessage", chat_id=555) is not None, "tapping a saved reply sends it to the customer")

    # legacy command still works
    await feed(upd(chat=GCHAT, from_user=ADMIN, text="/set_links New links here",
                   entities=[T.MessageEntity(type="bot_command", offset=0, length=10)]))
    check((await B.get_core("contact"))["text"] == "New links here", "old /set_links command still works")

    # 8) quiet follow-up
    await B.db_run("UPDATE users SET last_customer_at=?, last_agent_at=NULL WHERE user_id=555",
                   ((datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(timespec="seconds"),))
    SENT.clear()
    await B.run_quiet_check()
    check("Demo ID" in (last("SendMessage", chat_id=555) or {}).get("text", ""), "follow-up sent after 15 min of silence")
    SENT.clear()
    await B.run_quiet_check()
    check(last("SendMessage", chat_id=555) is None, "follow-up sent only once")

    # 9) CRM API
    async with TestClient(TestServer(B.app)) as c:
        r = await c.get("/api/bot-messages")
        check(r.status == 401, "API locked without login")
        r = await c.post("/api/login", json={"password": "wrong"})
        check(r.status == 401, "wrong password rejected")
        r = await c.post("/api/login", json={"password": "secret123"})
        check(r.status == 200, "login works")
        r = await c.get("/api/bot-messages"); data = await r.json()
        check(r.status == 200 and len(data) >= 15 and "when" in data[0], "list bot messages")
        fb = next(m for m in data if m["kind"] == "fallback")
        r = await c.delete(f"/api/bot-messages/{fb['id']}")
        check(r.status == 400, "core message can't be deleted")
        r = await c.post("/api/bot-messages", json={"kind": "keywords", "keywords": [], "text": "x"})
        check(r.status == 400 and "word" in (await r.json())["error"], "validation: keywords required")
        r = await c.post("/api/bot-messages", json={"kind": "keywords", "name": "Refund", "keywords": ["refund", "paisa wapas"],
                                                    "text": "Refund 24 ghante mein", "buttons": [{"label": "Help", "url": "https://x.com"}]})
        made = await r.json()
        check(r.status == 200 and made["keywords"] == ["refund", "paisa wapas"], "create keyword message")
        r = await c.post("/api/bot-messages", json={"kind": "saved", "text": "hi", "buttons": [{"label": "Bad", "url": "x.com"}]})
        check(r.status == 400, "validation: button link must start with https://")
        r = await c.put(f"/api/bot-messages/{made['id']}", json={"is_active": False})
        check((await r.json())["is_active"] is False, "toggle off")
        r = await c.post("/api/simulate", json={"text": "refund chahiye"})
        sim = await r.json()
        check(sim["message"]["kind"] == "fallback", "switched-off message is skipped by Try it")
        r = await c.delete(f"/api/bot-messages/{made['id']}"); deleted = (await r.json())["deleted"]
        r = await c.post("/api/bot-messages/restore", json=deleted)
        check(r.status == 200 and (await r.json())["name"] == "Refund", "undo delete restores message")
        r = await c.post("/api/simulate", json={"text": "kyc"}); sim = await r.json()
        check(sim["will_send"] and "kyc" in sim["reason"].lower(), "simulate explains match")
        r = await c.post("/api/simulate", json={"event": "start"}); sim = await r.json()
        check("Namaste Rahul" in sim["preview_text"], "simulate start renders preview")
        # media upload
        from aiohttp import FormData
        fd = FormData(); fd.add_field("file", b"\x89PNG fake", filename="banner.png", content_type="image/png")
        r = await c.post(f"/api/bot-messages/{start['id']}/media", data=fd); m = await r.json()
        check(m["media"] and m["media"]["kind"] == "photo", "picture attached to welcome")
        r = await c.get(m["media"]["url"])
        check(r.status == 200 and (await r.read()).startswith(b"\x89PNG"), "picture served to CRM")
        SENT.clear()
        await feed(upd(chat=PCHAT, from_user=CUST, text="/start", entities=[T.MessageEntity(type="bot_command", offset=0, length=6)]))
        sp = last("SendPhoto", chat_id=555)
        check(sp and "Namaste Rahul" in sp["caption"] and "keyboard" in sp["reply_markup"], "welcome sent as photo + caption + share button")
        mrow = await B.db_one("SELECT file_id FROM media WHERE id=?", (m["media"]["id"],))
        check(mrow["file_id"] == "tgphoto123", "telegram file_id cached after first send")
        for path in ("/api/stats", "/api/chats", "/api/chats/555/messages", "/api/vault", "/api/settings", "/api/audience-counts", "/api/export/leads.csv", "/health"):
            r = await c.get(path)
            check(r.status == 200, f"GET {path}")
        chats = await (await c.get("/api/chats")).json()
        pay = next(x for x in chats if x["user_id"] == 555)["payment"]
        check(pay and pay["status"] == "Pending Verification" and pay["image_url"].startswith("/api/vault/"), "chat list carries the latest payment to check")
        r = await c.put(f"/api/vault/{pay['id']}", json={"status": "Verified"})
        chats = await (await c.get("/api/chats")).json()
        check(next(x for x in chats if x["user_id"] == 555)["payment"]["status"] == "Verified", "verifying from Chats updates the payment")
        r = await c.get("/fonts/ibm-plex-sans.woff2"); body = await r.read()
        check(r.status == 200 and body[:4] == b"wOF2", "self-hosted font served")
        r = await c.get("/fonts/..%2Fbot.py"); check(r.status == 404, "font route refuses other files")
        msgs = await (await c.get("/api/chats/555/messages")).json()
        check(any(x["rule_name"] == "Deposit help" for x in msgs), "chat history labels auto-replies")
        r = await c.put("/api/settings", json={"handoff_minutes": 5}); check((await r.json())["handoff_minutes"] == "5", "settings saved")
        r = await c.post("/api/chats/555/send-saved", json={"message_id": saved["id"]}); check(r.status == 200, "send saved reply from CRM")
        st = await (await c.get("/api/stats")).json()
        check(st["auto_today"] >= 3 and st["unanswered_today"] >= 1, f"stats count auto-replies ({st['auto_today']}) and unanswered")
        r = await c.get("/"); check(r.status == 200, "CRM page served")
        # bot display name
        r = await c.get("/api/bot-name"); check(r.status == 200 and (await r.json())["name"] == "Prototype Bot", "read the bot's Telegram name")
        r = await c.put("/api/bot-name", json={"name": "  Play 246   Support "})
        check(r.status == 200 and (await r.json())["name"] == "Play 246 Support" and BOT_NAME["name"] == "Play 246 Support", "rename the bot (spaces tidied)")
        r = await c.put("/api/bot-name", json={"name": "   "}); check(r.status == 400, "empty bot name rejected")
        r = await c.put("/api/bot-name", json={"name": "x" * 65}); check(r.status == 400 and "64" in (await r.json())["error"], "bot name over 64 characters rejected")
        BOT_NAME["rate_limited"] = True
        r = await c.put("/api/bot-name", json={"name": "Another"}); err = (await r.json())["error"]
        check(r.status == 502 and "5 minutes" in err and BOT_NAME["name"] == "Play 246 Support", "Telegram rate limit explained in plain words")
        BOT_NAME["rate_limited"] = False
    print(f"\nALL {ok} CHECKS PASSED")

asyncio.run(main())
