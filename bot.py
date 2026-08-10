import os, json, hmac, hashlib, time, sqlite3, asyncio, re
from urllib.parse import parse_qsl
from contextlib import closing

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.utils.keyboard import ReplyKeyboardBuilder
import uvicorn

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Botbyteamak_bot").lstrip("@")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
REFERRAL_REWARD = float(os.getenv("REFERRAL_REWARD", "3"))
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "20"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB = os.getenv("DB_PATH", "bot.db")
REQUIRE_PERSISTENT_DB = os.getenv("REQUIRE_PERSISTENT_DB", "0").lower() in ("1", "true", "yes")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
DB_BACKEND = "postgres" if DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")) else "sqlite"

CUSTOM_EMOJIS = {
    "balance": "🎁",
    "payout": "🏦",
    "gift": "🎉",
    "refer": "🎀",
    "withdraw": "🚀",
    "earn_more": "💸",
    "leaderboard": "🏆",
}

MESSAGE_DEFAULTS = {
    "welcome": "🎉 Welcome to BotByte Refer & Earn Bot!\n\nEarn ₹{reward} for every successful referral.\nMinimum withdrawal: ₹{min_withdrawal}.",
    "balance": "💰 Balance: ₹{balance:.2f}\n💵 Total earned: ₹{total_earned:.2f}\n👥 Referrals: {referrals}",
    "refer": "💕 Refer & Earn\n\nEarn ₹{reward} for each successful referral.\n\nYour link:\n{referral_link}\n\nShare it with your friends!",
    "my_invites": "🚀 My Invites\n\nYour successful referrals: {referrals}",
    "invitation_contest": "🎯 Invitation Contest\n\nInvite more users and climb the leaderboard!",
    "refer_tracker": "🔎 Refer Tracker\n\n😶 {started} Users Started From Your Link\n\n🔍 {not_joined} Users Haven’t Joined Channels\n\n👑 Verified And Credited From : {credited}",
    "payout": "Choose Desired Payment Method\nFrom Below 👇\n\nYour Current Bank:\nAccount Number: {bank_account}\nIFSC Code: {bank_ifsc}\n\nYour Current UPI: {upi}",
    "payout_upi": "💳 Send your UPI ID now.\n\nExample: yourname@upi",
    "payout_bank": "🏦 Send your bank details in this format:\n\nAccount-number/IFSC\n\nExample: 1234567890/SBIN0001234",
    "withdraw": "🚀 Send the amount you want to withdraw.\nMinimum: ₹{min_withdrawal}\nExample: 20",
    "withdrawal_history": "📜 Withdrawal History\n\n{lines}",
    "bot_fund": "💸 Bot Fund\n\nCurrent Bot Fund: ₹{fund:.2f}\nTotal user balance: ₹{user_balance:.2f}\nTotal paid withdrawals: ₹{paid:.2f}",
    "gift": "🎉 Send your gift code now. Example: BONUS20",
    "earn_more": "🤑 Earn More\n\nMore earning tasks can be added here by the admin.",
    "leaderboard": "🏆 Top Referrers\n\n{lines}",
}

def clean_message_template(text):
    # Admin may type \n; convert it to a real Telegram newline.
    return str(text or "").replace("\\n", "\n").strip()

def get_message(key, **kwargs):
    with closing(db()) as con:
        row = con.execute("SELECT message FROM message_settings WHERE key=?", (key,)).fetchone()
    text = row[0] if row and row[0] else MESSAGE_DEFAULTS.get(key, "")
    text = clean_message_template(text)
    try:
        return text.format(**kwargs)
    except Exception:
        return text


def get_custom_emoji(key):
    """Return saved Telegram custom-emoji ID if configured, otherwise fallback Unicode emoji."""
    with closing(db()) as con:
        row = con.execute("SELECT custom_emoji_id FROM emoji_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row and row[0] else CUSTOM_EMOJIS.get(key, "")

def save_custom_emoji(key, emoji_id):
    with closing(db()) as con:
        con.execute(
            "INSERT INTO emoji_settings(key, custom_emoji_id) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET custom_emoji_id=excluded.custom_emoji_id",
            (key, emoji_id),
        )
        con.commit()


def get_setting(key, default=""):
    with closing(db()) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def save_setting(key, value):
    with closing(db()) as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        con.commit()


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

class PostgresCompat:
    """Small compatibility wrapper so the existing SQLite-style '?' SQL also works on Postgres."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        sql = re.sub(r"\?", "%s", sql)
        return self._conn.execute(sql, params)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


def db():
    if DB_BACKEND == "postgres":
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires psycopg[binary].") from exc
        return PostgresCompat(psycopg.connect(DATABASE_URL, connect_timeout=10))
    return sqlite3.connect(DB)

def init_db():
    if DB_BACKEND == "postgres":
        with closing(db()) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS users(
                id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance DOUBLE PRECISION NOT NULL DEFAULT 0,
                total_earned DOUBLE PRECISION NOT NULL DEFAULT 0,
                referred_by BIGINT,
                referrals INTEGER NOT NULL DEFAULT 0,
                payout_method TEXT,
                payout_value TEXT,
                created_at BIGINT NOT NULL,
                leaderboard_visible INTEGER NOT NULL DEFAULT 1
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS transactions(
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL, type TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL,
                note TEXT, created_at BIGINT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL, amount DOUBLE PRECISION NOT NULL, method TEXT, value TEXT,
                status TEXT NOT NULL DEFAULT 'pending', created_at BIGINT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS gift_codes(
                code TEXT PRIMARY KEY, amount DOUBLE PRECISION NOT NULL,
                max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS gift_redemptions(
                code TEXT NOT NULL, user_id BIGINT NOT NULL, PRIMARY KEY(code,user_id)
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS emoji_settings(
                key TEXT PRIMARY KEY, custom_emoji_id TEXT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS message_settings(
                key TEXT PRIMARY KEY, message TEXT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS earn_more_items(
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, title TEXT NOT NULL,
                message TEXT NOT NULL, reward DOUBLE PRECISION NOT NULL DEFAULT 0, url TEXT,
                active INTEGER NOT NULL DEFAULT 1, created_at BIGINT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )""")
            # Safe for databases created by an earlier version without this column.
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS leaderboard_visible INTEGER NOT NULL DEFAULT 1")
            con.commit()
        return

    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            balance REAL NOT NULL DEFAULT 0, total_earned REAL NOT NULL DEFAULT 0,
            referred_by INTEGER, referrals INTEGER NOT NULL DEFAULT 0,
            payout_method TEXT, payout_value TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, type TEXT NOT NULL,
            amount REAL NOT NULL, note TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL,
            method TEXT, value TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gift_codes(
            code TEXT PRIMARY KEY, amount REAL NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gift_redemptions(
            code TEXT NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(code,user_id)
        );
        CREATE TABLE IF NOT EXISTS emoji_settings(
            key TEXT PRIMARY KEY, custom_emoji_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_settings(
            key TEXT PRIMARY KEY, message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS earn_more_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, message TEXT NOT NULL,
            reward REAL NOT NULL DEFAULT 0, url TEXT, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "leaderboard_visible" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN leaderboard_visible INTEGER NOT NULL DEFAULT 1")
        con.commit()

def get_user(uid):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def ensure_user(tg_user, referrer_id=None):
    uid = tg_user.id
    now = int(time.time())
    with closing(db()) as con:
        row = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            con.execute("UPDATE users SET username=?, first_name=? WHERE id=?",
                        (tg_user.username or "", tg_user.first_name or "", uid))
        else:
            valid_ref = referrer_id if referrer_id and referrer_id != uid and con.execute(
                "SELECT id FROM users WHERE id=?", (referrer_id,)).fetchone() else None
            con.execute("""INSERT INTO users
                (id,username,first_name,referred_by,created_at)
                VALUES(?,?,?,?,?)""",
                (uid, tg_user.username or "", tg_user.first_name or "", valid_ref, now))
            if valid_ref:
                con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+?, referrals=referrals+1 WHERE id=?",
                            (REFERRAL_REWARD, REFERRAL_REWARD, valid_ref))
                con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                            (valid_ref, "referral", REFERRAL_REWARD, f"Referral: {uid}", now))
        con.commit()


def extract_custom_emoji_id(message):
    """Read the first custom_emoji entity from a Telegram message."""
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for ent in entities:
        if getattr(ent, "type", "") == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            return ent.custom_emoji_id
    return None

def add_tx(uid, typ, amount, note=""):
    with closing(db()) as con:
        con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                    (uid, typ, amount, note, int(time.time())))
        con.commit()

def verify_init_data(init_data: str):
    if not init_data:
        raise HTTPException(401, "Telegram initData required")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Invalid initData")
    check = "\n".join(f"{k}={v}" for k,v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise HTTPException(401, "Invalid Telegram signature")
    auth_date = int(data.get("auth_date", "0"))
    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(401, "Expired Telegram data")
    user = json.loads(data.get("user", "{}"))
    if not user.get("id"):
        raise HTTPException(401, "Telegram user missing")
    return user

app = FastAPI(title="BotByte Referral Bot")
tg_bot = None

@app.get("/")
async def home():
    return FileResponse("index.html")

@app.get("/admin")
async def admin_page():
    return FileResponse("admin.html")

def require_admin(init_data):
    u = verify_init_data(init_data)
    if int(u["id"]) not in ADMIN_IDS:
        raise HTTPException(403, "Admin access required")
    return int(u["id"])


@app.get("/api/admin/emojis")
async def admin_emojis(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("SELECT key,custom_emoji_id FROM emoji_settings ORDER BY key").fetchall()
    return {"emojis": [{"key": k, "custom_emoji_id": v} for k, v in rows]}

@app.post("/api/admin/emojis")
async def admin_save_emoji(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    key = str(payload.get("key", "")).strip().lower()
    emoji_id = str(payload.get("custom_emoji_id", "")).strip()
    valid = {"balance","payout","gift","refer","withdraw","earn_more","leaderboard"}
    if key not in valid or not emoji_id:
        raise HTTPException(400, "Invalid emoji key or custom emoji ID")
    save_custom_emoji(key, emoji_id)
    return {"ok": True, "key": key, "custom_emoji_id": emoji_id}

@app.get("/api/messages")
async def public_messages():
    with closing(db()) as con:
        rows = con.execute("SELECT key,message FROM message_settings").fetchall()
    data = dict(rows)
    return {"messages": {k: clean_message_template(data.get(k, v)) for k,v in MESSAGE_DEFAULTS.items()}}

@app.get("/api/admin/messages")
async def admin_messages(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("SELECT key,message FROM message_settings ORDER BY key").fetchall()
    data = dict(rows)
    return {"messages": {k: data.get(k, v) for k,v in MESSAGE_DEFAULTS.items()}}

@app.post("/api/admin/messages")
async def admin_save_messages(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    allowed = set(MESSAGE_DEFAULTS)
    messages = payload.get("messages") or {}
    # Backward-compatible: accept the older {key, text} admin UI payload too.
    if not messages and payload.get("key"):
        messages = {str(payload.get("key")): payload.get("text", "")}
    if not isinstance(messages, dict):
        raise HTTPException(400, "messages must be an object")
    with closing(db()) as con:
        for key, value in messages.items():
            if key not in allowed:
                continue
            text = clean_message_template(value)
            if not text:
                continue
            if len(text) > 4096:
                raise HTTPException(400, f"{key} message is too long")
            con.execute("INSERT INTO message_settings(key,message) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET message=excluded.message", (key,text))
        con.commit()
    return {"ok": True}

@app.get("/api/admin/bot-fund")
async def admin_bot_fund(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    try: fund=float(get_setting("bot_fund","0"))
    except: fund=0.0
    return {"fund":fund}

@app.post("/api/admin/bot-fund")
async def admin_save_bot_fund(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    try: fund=float(payload.get("fund",0))
    except: raise HTTPException(400,"Invalid bot fund")
    if fund < 0: raise HTTPException(400,"Bot fund cannot be negative")
    save_setting("bot_fund", fund)
    return {"ok":True,"fund":fund}

@app.get("/api/admin/overview")
async def admin_overview(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    with closing(db()) as con:
        users_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        balance = con.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        earned = con.execute("SELECT COALESCE(SUM(total_earned),0) FROM users").fetchone()[0]
        withdrawn = con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status IN ('paid','approved')").fetchone()[0]
        bot_fund = float(get_setting("bot_fund","0") or 0)
        wr = con.execute("""SELECT w.id,w.user_id,w.amount,w.method,w.value,w.status,w.created_at,
                 COALESCE(NULLIF(u.username,''),NULLIF(u.first_name,''),'User')
                 FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id
                 WHERE w.status IN ('pending','processing') ORDER BY w.id DESC LIMIT 100""").fetchall()
        ur = con.execute("""SELECT id,COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User'),
                 referrals,balance,total_earned,leaderboard_visible FROM users ORDER BY id DESC LIMIT 100""").fetchall()
    return {
        "users":users_count,"balance":balance,"earned":earned,"withdrawn":withdrawn,"bot_fund":bot_fund,
        "withdrawals":[{"id":r[0],"user_id":r[1],"amount":r[2],"method":r[3],"value":r[4],"status":r[5],"created_at":r[6],"user":r[7]} for r in wr],
        "user_list":[{"id":r[0],"user":r[1],"referrals":r[2],"balance":r[3],"earned":r[4],"leaderboard_visible":bool(r[5])} for r in ur]
    }

@app.post("/api/admin/withdrawal")
async def admin_withdrawal(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    wid = int(payload.get("id", 0))
    action = str(payload.get("action", "")).strip().lower()
    if action == "approve":
        action = "processing"
    if action not in ("processing", "paid", "reject"):
        raise HTTPException(400, "Invalid withdrawal action")
    notify_uid = None
    notify_amount = 0
    with closing(db()) as con:
        row = con.execute("SELECT user_id,amount,status FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row:
            raise HTTPException(404, "Withdrawal not found")
        old_status = row[2]
        if old_status == "rejected":
            raise HTTPException(400, "Withdrawal was rejected")
        if action == "processing":
            if old_status != "pending":
                raise HTTPException(400, "Only pending withdrawals can be moved to processing")
            con.execute("UPDATE withdrawals SET status='processing' WHERE id=?", (wid,))
            note = f"Withdrawal #{wid} is processing"
        elif action == "paid":
            if old_status != "processing":
                raise HTTPException(400, "Move withdrawal to processing first")
            con.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
            now = int(time.time())
            con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                        (row[0], "withdrawal_paid", 0, f"Withdrawal #{wid} paid", now))
            note = f"Withdrawal #{wid} marked paid"
            notify_uid, notify_amount = row[0], row[1]
        else:
            if old_status not in ("pending", "processing"):
                raise HTTPException(400, "Withdrawal already completed")
            now = int(time.time())
            con.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
            con.execute("UPDATE users SET balance=balance+? WHERE id=?", (row[1], row[0]))
            con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                        (row[0], "withdrawal_refund", row[1], f"Withdrawal #{wid} rejected", now))
            note = f"Withdrawal #{wid} rejected and refunded"
        con.commit()
    if notify_uid and tg_bot:
        try:
            await tg_bot.send_message(notify_uid, f"✅ Withdrawal completed!\n\nWithdrawal #{wid} of ₹{notify_amount:g} has been marked as paid. 💸")
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        except Exception:
            pass
    return {"ok": True, "status": action, "message": note}

@app.post("/api/admin/gift")
async def admin_create_gift(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    code=str(payload.get("code","" )).strip().upper(); amount=float(payload.get("amount",0)); uses=int(payload.get("max_uses",1))
    if not code or amount<=0 or uses<1: raise HTTPException(400,"Invalid gift details")
    with closing(db()) as con:
        con.execute("INSERT INTO gift_codes(code,amount,max_uses,uses) VALUES(?,?,?,0) ON CONFLICT(code) DO UPDATE SET amount=excluded.amount,max_uses=excluded.max_uses,uses=0",(code,amount,uses))
        con.commit()
    return {"ok":True,"code":code,"amount":amount,"max_uses":uses}

@app.post("/api/admin/broadcast")
async def admin_broadcast(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    global tg_bot
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "Broadcast message is empty")
    if len(text) > 4096:
        raise HTTPException(400, "Message is too long (max 4096 characters)")
    if tg_bot is None:
        raise HTTPException(503, "Bot is still starting. Try again in a few seconds.")

    with closing(db()) as con:
        user_ids = [r[0] for r in con.execute("SELECT id FROM users ORDER BY id").fetchall()]

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await tg_bot.send_message(uid, text)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    return {"ok": True, "total": len(user_ids), "sent": sent, "failed": failed}

@app.post("/api/admin/leaderboard")
async def admin_leaderboard(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    uid = int(payload.get("user_id", 0))
    visible = 1 if bool(payload.get("visible", True)) else 0
    if uid <= 0:
        raise HTTPException(400, "Invalid user ID")
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            raise HTTPException(404, "User not found")
        con.execute("UPDATE users SET leaderboard_visible=? WHERE id=?", (visible, uid))
        con.commit()
    return {"ok": True, "user_id": uid, "visible": bool(visible)}

@app.post("/api/admin/earn-more")
async def admin_earn_more(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    title = str(payload.get("title", "")).strip()
    message = str(payload.get("message", "")).strip()
    url = str(payload.get("url", "")).strip() or None
    try:
        reward = float(payload.get("reward", 0) or 0)
    except Exception:
        raise HTTPException(400, "Invalid reward")
    if not title or not message:
        raise HTTPException(400, "Title and message are required")
    if reward < 0:
        raise HTTPException(400, "Reward cannot be negative")
    with closing(db()) as con:
        if DB_BACKEND == "postgres":
            cur = con.execute("INSERT INTO earn_more_items(title,message,reward,url,active,created_at) VALUES(?,?,?,?,1,?) RETURNING id",
                              (title, message, reward, url, int(time.time())))
            new_id = cur.fetchone()[0]
        else:
            cur = con.execute("INSERT INTO earn_more_items(title,message,reward,url,active,created_at) VALUES(?,?,?,?,1,?)",
                              (title, message, reward, url, int(time.time())))
            new_id = cur.lastrowid
        con.commit()
    return {"ok": True, "id": new_id}

@app.get("/api/admin/earn-more")
async def admin_earn_more_list(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,message,reward,url,active,created_at FROM earn_more_items ORDER BY id DESC LIMIT 100").fetchall()
    return {"items": [{"id":r[0],"title":r[1],"message":r[2],"reward":r[3],"url":r[4],"active":bool(r[5]),"created_at":r[6]} for r in rows]}

@app.post("/api/admin/earn-more/toggle")
async def admin_earn_more_toggle(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    item_id = int(payload.get("id", 0))
    active = 1 if bool(payload.get("active", True)) else 0
    with closing(db()) as con:
        con.execute("UPDATE earn_more_items SET active=? WHERE id=?", (active, item_id))
        con.commit()
    return {"ok": True}

@app.post("/api/admin/earn-more/delete")
async def admin_earn_more_delete(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    item_id = int(payload.get("id", 0))
    with closing(db()) as con:
        con.execute("DELETE FROM earn_more_items WHERE id=?", (item_id,))
        con.commit()
    return {"ok": True}

@app.get("/api/earn-more")
async def earn_more_api(x_telegram_init_data: str | None = Header(default=None)):
    verify_init_data(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,message,reward,url FROM earn_more_items WHERE active=1 ORDER BY id DESC").fetchall()
    return {"items": [{"id":r[0],"title":r[1],"message":r[2],"reward":r[3],"url":r[4]} for r in rows]}

@app.post("/api/admin/balance")
async def admin_balance(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    uid=int(payload.get("user_id",0)); amount=float(payload.get("amount",0))
    if uid<=0 or amount==0: raise HTTPException(400,"Enter a non-zero balance adjustment")
    with closing(db()) as con:
        row=con.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
        if not row: raise HTTPException(404,"User not found")
        new_balance=row[0]+amount
        if new_balance < 0: raise HTTPException(400,"Balance cannot go below ₹0")
        if amount > 0:
            con.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(amount,amount,uid))
            typ,note="admin_credit","Admin panel credit"
        else:
            con.execute("UPDATE users SET balance=balance+? WHERE id=?",(amount,uid))
            typ,note="admin_debit","Admin panel balance cut"
        con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",(uid,typ,amount,note,int(time.time())))
        con.commit()
    return {"ok":True,"amount":amount,"balance":new_balance}

@app.get("/api/me")
async def me(x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data)
    uid = int(u["id"])
    with closing(db()) as con:
        row = con.execute("""SELECT balance,total_earned,referrals,payout_method,payout_value
                             FROM users WHERE id=?""", (uid,)).fetchone()
        tx = con.execute("""SELECT type,amount,note,created_at FROM transactions
                            WHERE user_id=? ORDER BY id DESC LIMIT 20""", (uid,)).fetchall()
        wd = con.execute("""SELECT id,amount,method,value,status,created_at
                            FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20""", (uid,)).fetchall()
    if not row:
        raise HTTPException(404, "User not registered")
    return {
        "user": {"id": uid, "name": u.get("first_name",""), "username": u.get("username","")},
        "balance": row[0], "total_earned": row[1], "referrals": row[2],
        "payout_method": row[3], "payout_value": row[4],
        "referral_link": f"https://t.me/{BOT_USERNAME}?start={uid}",
        "transactions": [{"type":a,"amount":b,"note":c,"created_at":d} for a,b,c,d in tx],
        "withdrawals": [{"id":a,"amount":b,"method":c,"value":d,"status":e,"created_at":f}
                        for a,b,c,d,e,f in wd],
        "reward": REFERRAL_REWARD, "min_withdrawal": MIN_WITHDRAWAL
    }

@app.get("/api/history")
async def history(x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data)
    uid = int(u["id"])
    with closing(db()) as con:
        rows = con.execute("""
            SELECT type, amount, note, created_at
            FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 100
        """, (uid,)).fetchall()
    return {"history": [
        {"type": r[0], "amount": r[1], "note": r[2], "created_at": r[3]}
        for r in rows
    ]}

@app.get("/api/leaderboard")
async def leaderboard():
    with closing(db()) as con:
        rows = con.execute("""
            SELECT u.id, COALESCE(NULLIF(u.username,''), NULLIF(u.first_name,''), 'User') AS display_name,
                   u.referrals, COALESCE(SUM(w.amount),0) AS total_withdrawal
            FROM users u
            LEFT JOIN withdrawals w ON w.user_id=u.id
            WHERE u.leaderboard_visible=1
            GROUP BY u.id
            ORDER BY u.referrals DESC, total_withdrawal DESC, u.created_at ASC
            LIMIT 20
        """).fetchall()
    return {"leaderboard":[{"user_id":r[0],"user":r[1],"referrals":r[2],"total_withdrawal":r[3]} for r in rows]}

@app.get("/api/bot-fund")
async def bot_fund_api(x_telegram_init_data: str | None = Header(default=None)):
    verify_init_data(x_telegram_init_data)
    with closing(db()) as con:
        user_balance=con.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        paid=con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status IN ('paid','approved')").fetchone()[0]
    try: fund=float(get_setting("bot_fund","0"))
    except: fund=0.0
    return {"fund":fund,"user_balance":user_balance,"paid":paid}

@app.get("/api/refer-details")
async def refer_details(x_telegram_init_data: str | None = Header(default=None)):
    u=verify_init_data(x_telegram_init_data); uid=int(u["id"])
    with closing(db()) as con:
        users=con.execute("SELECT COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User') FROM users WHERE referred_by=? ORDER BY id DESC LIMIT 50",(uid,)).fetchall()
        credited=con.execute("SELECT COUNT(*) FROM transactions WHERE user_id=? AND type='referral'",(uid,)).fetchone()[0]
        board=con.execute("SELECT COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User'),referrals FROM users WHERE leaderboard_visible=1 ORDER BY referrals DESC,created_at ASC LIMIT 10").fetchall()
    return {"referrals":len(users),"users":[{"name":r[0]} for r in users],"started":len(users),"not_joined":0,"credited":credited,"leaderboard":[{"name":r[0],"referrals":r[1]} for r in board]}

@app.post("/api/payout")
async def payout(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data); uid = int(u["id"])
    method = str(payload.get("method","")).strip()
    value = str(payload.get("value","")).strip()
    if not method or not value:
        raise HTTPException(400, "Payout method and value are required")
    with closing(db()) as con:
        con.execute("UPDATE users SET payout_method=?, payout_value=? WHERE id=?", (method,value,uid))
        con.commit()
    return {"ok": True}

@app.post("/api/withdraw")
async def withdraw(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data); uid = int(u["id"])
    try: amount = float(payload.get("amount"))
    except: raise HTTPException(400, "Invalid amount")
    if amount < MIN_WITHDRAWAL:
        raise HTTPException(400, f"Minimum withdrawal is ₹{MIN_WITHDRAWAL:g}")
    with closing(db()) as con:
        row = con.execute("SELECT balance,payout_method,payout_value FROM users WHERE id=?", (uid,)).fetchone()
        if not row or row[0] < amount:
            raise HTTPException(400, "Insufficient balance")
        if not row[1] or not row[2]:
            raise HTTPException(400, "Set payout method first")
        con.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount,uid))
        con.execute("""INSERT INTO withdrawals(user_id,amount,method,value,status,created_at)
                       VALUES(?,?,?,?,?,?)""", (uid,amount,row[1],row[2],"pending",int(time.time())))
        con.execute("""INSERT INTO transactions(user_id,type,amount,note,created_at)
                       VALUES(?,?,?,?,?)""", (uid,"withdrawal",-amount,"Withdrawal request",int(time.time())))
        con.commit()
    return {"ok": True}

@app.get("/api/withdrawals")
async def user_withdrawals(x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data)
    uid = int(u["id"])
    with closing(db()) as con:
        rows = con.execute("""SELECT id,amount,method,value,status,created_at
                              FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20""",
                           (uid,)).fetchall()
    return {"withdrawals": [
        {"id": r[0], "amount": r[1], "method": r[2], "value": r[3],
         "status": r[4], "created_at": r[5]}
        for r in rows
    ]}

@app.post("/api/gift")
async def gift(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data); uid = int(u["id"])
    code = str(payload.get("code","")).strip().upper()
    if not code: raise HTTPException(400,"Enter a gift code")
    with closing(db()) as con:
        g = con.execute("SELECT amount,max_uses,uses FROM gift_codes WHERE code=?", (code,)).fetchone()
        if not g or g[2] >= g[1]:
            raise HTTPException(400,"Invalid or exhausted gift code")
        if con.execute("SELECT 1 FROM gift_redemptions WHERE code=? AND user_id=?", (code,uid)).fetchone():
            raise HTTPException(400,"Code already redeemed")
        con.execute("INSERT INTO gift_redemptions(code,user_id) VALUES(?,?)",(code,uid))
        con.execute("UPDATE gift_codes SET uses=uses+1 WHERE code=?", (code,))
        con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?",(g[0],g[0],uid))
        con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                    (uid,"gift",g[0],code,int(time.time())))
        con.commit()
    return {"ok": True, "amount": g[0]}

async def send_home(message: Message):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎁 Balance")
    kb.button(text="💕 Refer & Earn")
    kb.button(text="🎉 Gift Code")
    kb.button(text="🚀 Withdraw")
    kb.button(text="🏦 Payout Method")
    kb.button(text="🤑 Earn More")
    kb.button(text="🏆 Leaderboard")
    kb.adjust(2,2,2)
    inline_rows = []
    if WEBAPP_URL:
        inline_rows.append([InlineKeyboardButton(text="📱 Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
    inline = InlineKeyboardMarkup(inline_keyboard=inline_rows) if inline_rows else None
    text = get_message("welcome", reward=REFERRAL_REWARD, min_withdrawal=MIN_WITHDRAWAL)
    await message.answer(text, reply_markup=kb.as_markup(resize_keyboard=True))
    if inline:
        await message.answer("Open your dashboard:", reply_markup=inline)

dp = Dispatcher()
pending_action = {}  # uid -> next bot action (payout / withdraw / gift)


@dp.message(CommandStart())
async def start(message: Message):
    ref = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit():
        ref = int(parts[1])
    ensure_user(message.from_user, ref)
    await send_home(message)


@dp.message(Command("setemoji"))
async def setemoji_start(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "🎨 Custom Emoji Manager\n\n"
        "Send a Premium/custom emoji as the next message.\n"
        "Before sending it, use this command with the button name:\n\n"
        "/setemoji balance\n"
        "/setemoji payout\n"
        "/setemoji gift\n"
        "/setemoji refer\n"
        "/setemoji withdraw\n"
        "/setemoji earn_more\n"
        "/setemoji leaderboard\n\n"
        "Then send the custom emoji alone."
    )
    pending_action[message.from_user.id] = "setemoji:" + ((message.text or "").split(maxsplit=1)[1].lower()
                                                          if len((message.text or "").split(maxsplit=1)) == 2 else "")

@dp.message(Command("adminpanel"))
async def adminpanel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("❌ You are not authorized to use the admin panel.")
    if not WEBAPP_URL:
        return await message.answer("❌ Admin panel is not configured yet.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛠 Open Admin Panel", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))
    ]])
    await message.answer("🛠 Admin Panel\n\nTap below to open the admin Mini App.\n\n🎨 Custom Emoji: use /setemoji <button> then send the Premium emoji.", reply_markup=kb)

@dp.message(F.text == "🎁 Balance")
async def balance(message: Message):
    ensure_user(message.from_user)
    u = get_user(message.from_user.id)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Withdrawal History", callback_data="withdrawal_history")],
        [InlineKeyboardButton(text="💸 Bot Fund", callback_data="bot_fund")]
    ])
    await message.answer(get_message("balance", balance=u[3], total_earned=u[4], referrals=u[6]), reply_markup=kb)

@dp.callback_query(F.data == "withdrawal_history")
async def withdrawal_history(callback):
    uid=callback.from_user.id
    with closing(db()) as con:
        rows=con.execute("SELECT id,amount,status,method,value,created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20",(uid,)).fetchall()
    if rows:
        lines="\n".join(f"#{r[0]} — ₹{r[1]:g} — {str(r[2]).title()} — {r[3] or 'Payout'}" for r in rows)
    else:
        lines="No withdrawals yet."
    await callback.answer()
    await callback.message.answer(get_message("withdrawal_history", lines=lines))

@dp.callback_query(F.data == "bot_fund")
async def bot_fund(callback):
    with closing(db()) as con:
        user_balance=con.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        paid=con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status IN ('paid','approved')").fetchone()[0]
    try: fund=float(get_setting("bot_fund","0"))
    except: fund=0.0
    await callback.answer()
    await callback.message.answer(get_message("bot_fund", fund=fund, user_balance=user_balance, paid=paid))

@dp.message(F.text == "💕 Refer & Earn")
async def refer(message: Message):
    ensure_user(message.from_user)
    link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    text = (f"🎁 Per Invite ₹{REFERRAL_REWARD:g} UPI Cash !!\n\n"
            f"🎀 Invite Link : {link}\n\n"
            "🟢 Share Your Own Invite Link To Earn Unlimited Easy Cash! 🤑")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 My Invites", callback_data="ref_my_invites"),
         InlineKeyboardButton(text="🎯 Invitation Contest", callback_data="ref_contest")],
        [InlineKeyboardButton(text="🔎 Refer Tracker", callback_data="ref_tracker")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "ref_my_invites")
async def ref_my_invites(callback):
    uid=callback.from_user.id
    with closing(db()) as con:
        rows=con.execute("SELECT id,COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User') FROM users WHERE referred_by=? ORDER BY id DESC LIMIT 50",(uid,)).fetchall()
    refs=len(rows)
    if rows:
        lines="\n".join(f"{i}. {name} ({rid})" for i,(rid,name) in enumerate(rows,1))
    else:
        lines="No users have joined from your link yet."
    await callback.answer()
    await callback.message.answer(f"🚀 My Invites\n\nYour successful referrals: {refs}\n\n{lines}")

@dp.callback_query(F.data == "ref_contest")
async def ref_contest(callback):
    with closing(db()) as con:
        rows=con.execute("SELECT COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User'),referrals FROM users WHERE leaderboard_visible=1 ORDER BY referrals DESC,created_at ASC LIMIT 10").fetchall()
    lines="\n".join(f"{i}. {name} — {refs} invites" for i,(name,refs) in enumerate(rows,1)) or "No contest entries yet."
    await callback.answer()
    await callback.message.answer(f"🎯 Invitation Contest\n\nInvite more users and climb the leaderboard!\n\n{lines}")

@dp.callback_query(F.data == "ref_tracker")
async def ref_tracker(callback):
    uid=callback.from_user.id
    with closing(db()) as con:
        started=con.execute("SELECT COUNT(*) FROM users WHERE referred_by=?",(uid,)).fetchone()[0]
        credited=con.execute("SELECT COUNT(*) FROM transactions WHERE user_id=? AND type='referral'",(uid,)).fetchone()[0]
    await callback.answer()
    await callback.message.answer(get_message("refer_tracker", started=started, not_joined=0, credited=credited))

@dp.message(F.text == "🏦 Payout Method")
async def payout_method(message: Message):
    ensure_user(message.from_user)
    uid=message.from_user.id
    u=get_user(uid)
    bank_account="not set"; bank_ifsc="not set"; upi="not set"
    if u and u[7] and u[8]:
        method=str(u[7]).upper(); value=str(u[8])
        if method == "BANK":
            parts=re.split(r"[/-]", value, maxsplit=1)
            bank_account=parts[0].strip() or "not set"
            bank_ifsc=parts[1].strip() if len(parts)>1 and parts[1].strip() else "not set"
        elif method == "UPI":
            upi=value
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 UPI", callback_data="payout_upi"),
         InlineKeyboardButton(text="🏦 Bank Account", callback_data="payout_bank")]
    ])
    await message.answer(get_message("payout", bank_account=bank_account, bank_ifsc=bank_ifsc, upi=upi), reply_markup=kb)

@dp.callback_query(F.data == "payout_upi")
async def payout_upi_callback(callback):
    pending_action[callback.from_user.id]="payout_upi"
    await callback.answer()
    await callback.message.answer(get_message("payout_upi"))

@dp.callback_query(F.data == "payout_bank")
async def payout_bank_callback(callback):
    pending_action[callback.from_user.id]="payout_bank"
    await callback.answer()
    await callback.message.answer(get_message("payout_bank"))

@dp.message(F.text == "🏆 Leaderboard")
async def bot_leaderboard(message: Message):
    ensure_user(message.from_user)
    with closing(db()) as con:
        rows = con.execute("""
            SELECT COALESCE(NULLIF(u.username,''), NULLIF(u.first_name,''), 'User'),
                   u.referrals, COALESCE(SUM(w.amount),0)
            FROM users u LEFT JOIN withdrawals w ON w.user_id=u.id
            WHERE u.leaderboard_visible=1
            GROUP BY u.id
            ORDER BY u.referrals DESC, COALESCE(SUM(w.amount),0) DESC, u.created_at ASC
            LIMIT 10
        """).fetchall()
    if not rows:
        return await message.answer("🏆 Leaderboard\n\nNo users yet.")
    lines = []
    for i,(name,refs,wd) in enumerate(rows,1):
        lines.append(f"{i}. {name} — 👥 {refs} referrals — 💸 ₹{wd:.2f} withdrawn")
    await message.answer(get_message("leaderboard", lines="\n".join(lines)))

@dp.message(F.text == "🚀 Withdraw")
async def withdraw_help(message: Message):
    ensure_user(message.from_user)
    pending_action[message.from_user.id] = "withdraw"
    await message.answer(get_message("withdraw", min_withdrawal=MIN_WITHDRAWAL))

@dp.message(F.text == "🎉 Gift Code")
async def gift_help(message: Message):
    ensure_user(message.from_user)
    pending_action[message.from_user.id] = "gift"
    await message.answer(get_message("gift"))

@dp.message(F.text == "🤑 Earn More")
async def earn_more(message: Message):
    with closing(db()) as con:
        rows=con.execute("SELECT title,message,reward,url FROM earn_more_items WHERE active=1 ORDER BY id DESC").fetchall()
    if not rows:
        return await message.answer(get_message("earn_more"))
    parts=["🤑 Earn More\n"]
    for i,(title,body,reward,url) in enumerate(rows,1):
        line=f"{i}. {title}\n{body}"
        if reward>0: line += f"\n💰 Reward: ₹{reward:g}"
        if url: line += f"\n🔗 {url}"
        parts.append(line)
    await message.answer("\n\n".join(parts))

@dp.message(Command("addbalance"))
async def admin_addbalance(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    p = (message.text or "").split()
    if len(p) != 3: return await message.answer("/addbalance USER_ID AMOUNT")
    uid, amount = int(p[1]), float(p[2])
    with closing(db()) as con:
        con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?",(amount,amount,uid))
        con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",(uid,"admin_credit",amount,"Admin credit",int(time.time())))
        con.commit()
    await message.answer("Balance added.")

@dp.message(Command("gift"))
async def admin_gift(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    p = (message.text or "").split()
    if len(p) not in (3,4): return await message.answer("/gift CODE AMOUNT [MAX_USES]")
    code, amount = p[1].upper(), float(p[2]); uses = int(p[3]) if len(p)==4 else 1
    with closing(db()) as con:
        con.execute("INSERT INTO gift_codes(code,amount,max_uses,uses) VALUES(?,?,?,0) ON CONFLICT(code) DO UPDATE SET amount=excluded.amount,max_uses=excluded.max_uses,uses=0",(code,amount,uses))
        con.commit()
    await message.answer(f"Gift code created: {code} = ₹{amount:g}")

@dp.message(Command("withdrawals"))
async def admin_withdrawals(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    with closing(db()) as con:
        rows = con.execute("""SELECT id,user_id,amount,method,value,status FROM withdrawals
                              WHERE status IN ('pending','processing') ORDER BY id DESC LIMIT 20""").fetchall()
    if not rows: return await message.answer("No pending/processing withdrawals.")
    txt = "\n".join(f"#{r[0]} | user {r[1]} | ₹{r[2]:g} | {r[3]}: {r[4]} | {r[5]}" for r in rows)
    await message.answer("Withdrawals:\n\n"+txt)

@dp.message()
async def pending_actions(message: Message):
    uid = message.from_user.id
    action = pending_action.get(uid)
    if not action or not message.text:
        return
    text = message.text.strip()
    try:
        if action.startswith("setemoji:"):
            key = action.split(":", 1)[1]
            valid = {"balance","payout","gift","refer","withdraw","earn_more","leaderboard"}
            if key not in valid:
                await message.answer("❌ Invalid button name. Use /setemoji balance, payout, gift, refer, withdraw, earn_more or leaderboard.")
            else:
                emoji_id = extract_custom_emoji_id(message)
                if not emoji_id:
                    await message.answer("❌ Custom emoji nahi mila. Premium/custom emoji ko akela message karke bhejo.")
                else:
                    save_custom_emoji(key, emoji_id)
                    await message.answer(f"✅ Custom emoji saved for {key}: {emoji_id}")
            pending_action.pop(uid, None)
        elif action in ("payout", "payout_upi", "payout_bank"):
            if action == "payout_upi":
                method, value = "UPI", text
                if "@" not in value or len(value) < 5:
                    return await message.answer("❌ Please send a valid UPI ID. Example: yourname@upi")
            elif action == "payout_bank":
                method, value = "BANK", text
                if not re.match(r"^\S+[/\-]\S+$", value):
                    return await message.answer("❌ Format: Account-number/IFSC")
            else:
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or parts[0].lower() not in ("upi","bank"):
                    return await message.answer("❌ Format: UPI yourname@upi\nOr: Bank account-number/IFSC")
                method, value = parts[0].upper(), parts[1].strip()
            with closing(db()) as con:
                con.execute("UPDATE users SET payout_method=?, payout_value=? WHERE id=?", (method,value,uid))
                con.commit()
            await message.answer(f"✅ Payout method saved: {method} — {value}")
        elif action == "withdraw":
            amount = float(text)
            if amount < MIN_WITHDRAWAL:
                return await message.answer(f"❌ Minimum withdrawal is ₹{MIN_WITHDRAWAL:g}")
            with closing(db()) as con:
                row = con.execute("SELECT balance,payout_method,payout_value FROM users WHERE id=?", (uid,)).fetchone()
                if not row or row[0] < amount:
                    return await message.answer("❌ Insufficient balance.")
                if not row[1] or not row[2]:
                    return await message.answer("❌ Set your payout method first using 🏦 Payout Method.")
                now = int(time.time())
                con.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount,uid))
                con.execute("INSERT INTO withdrawals(user_id,amount,method,value,status,created_at) VALUES(?,?,?,?,?,?)",
                            (uid,amount,row[1],row[2],"pending",now))
                con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                            (uid,"withdrawal",-amount,"Withdrawal request",now))
                con.commit()
            await message.answer(f"✅ Withdrawal request submitted: ₹{amount:g}")
        elif action == "gift":
            code = text.upper()
            with closing(db()) as con:
                g = con.execute("SELECT amount,max_uses,uses FROM gift_codes WHERE code=?", (code,)).fetchone()
                if not g or g[2] >= g[1]:
                    return await message.answer("❌ Invalid or exhausted gift code.")
                if con.execute("SELECT 1 FROM gift_redemptions WHERE code=? AND user_id=?", (code,uid)).fetchone():
                    return await message.answer("❌ Code already redeemed.")
                now=int(time.time())
                con.execute("INSERT INTO gift_redemptions(code,user_id) VALUES(?,?)",(code,uid))
                con.execute("UPDATE gift_codes SET uses=uses+1 WHERE code=?", (code,))
                con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?",(g[0],g[0],uid))
                con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                            (uid,"gift",g[0],code,now))
                con.commit()
            await message.answer(f"🎉 Gift code redeemed! ₹{g[0]:g} added to your balance.")
        pending_action.pop(uid, None)
    except ValueError:
        await message.answer("❌ Please enter a valid number.")
    except Exception:
        await message.answer("❌ Something went wrong. Please try again.")
        pending_action.pop(uid, None)

async def main():
    global tg_bot
    if REQUIRE_PERSISTENT_DB and DB_BACKEND != "postgres":
        raise RuntimeError("Persistent database required. Set DATABASE_URL to a Render Postgres connection string before deploying.")
    if DB_BACKEND == "sqlite":
        print("WARNING: Using local SQLite bot.db. On Render this data is ephemeral. Set DATABASE_URL to Render Postgres.")
    else:
        print("Database backend: Render PostgreSQL (persistent)")
    init_db()
    bot = Bot(BOT_TOKEN)
    tg_bot = bot
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button={"type":"web_app","text":"📱 Dashboard","web_app":{"url":WEBAPP_URL}}
        )
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT","8080")), log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(dp.start_polling(bot), server.serve())

if __name__ == "__main__":
    asyncio.run(main())
