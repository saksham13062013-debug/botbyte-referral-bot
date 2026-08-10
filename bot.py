import os, json, hmac, hashlib, time, sqlite3, asyncio
from urllib.parse import parse_qsl
from contextlib import closing

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
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
DB = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

def db():
    return sqlite3.connect(DB)

def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            total_earned REAL NOT NULL DEFAULT 0,
            referred_by INTEGER,
            referrals INTEGER NOT NULL DEFAULT 0,
            payout_method TEXT,
            payout_value TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            method TEXT,
            value TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gift_codes(
            code TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gift_redemptions(
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(code,user_id)
        );
        CREATE TABLE IF NOT EXISTS force_join_channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            invite_link TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );
        """)
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

def add_tx(uid, typ, amount, note=""):
    with closing(db()) as con:
        con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                    (uid, typ, amount, note, int(time.time())))
        con.commit()

async def get_force_join_channels():
    with closing(db()) as con:
        return con.execute("SELECT id,chat_id,title,invite_link,active FROM force_join_channels WHERE active=1 ORDER BY id").fetchall()

async def force_join_status(user_id: int):
    channels = await get_force_join_channels()
    missing = []
    for cid, chat_id, title, invite_link, active in channels:
        try:
            member = await tg_bot.get_chat_member(chat_id, user_id)
            status = getattr(member, "status", "")
            is_member = getattr(member, "is_member", None)
            ok = status in ("creator", "administrator", "member") or (status == "restricted" and is_member is True)
            if not ok:
                missing.append((cid, str(chat_id), title or str(chat_id), invite_link))
        except Exception:
            # If Telegram cannot verify a configured channel, keep it as missing
            # rather than accidentally bypassing force-join.
            missing.append((cid, str(chat_id), title or str(chat_id), invite_link))
    return missing

async def enforce_force_join(message: Message):
    if message.from_user and message.from_user.id in ADMIN_IDS:
        return True
    missing = await force_join_status(message.from_user.id)
    if not missing:
        return True
    rows = []
    for _, chat_id, title, invite_link in missing:
        if invite_link:
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=invite_link)])
        elif str(chat_id).startswith("@"):
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=f"https://t.me/{str(chat_id)[1:]}")])
    rows.append([InlineKeyboardButton(text="🔄 I've Joined — Check Again", callback_data="force_join_check")])
    await message.answer(
        "🔒 <b>Join Required Channels</b>\n\nPlease join all required channels below, then tap <b>I've Joined — Check Again</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )
    return False

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
    return FileResponse("web/index.html")

@app.get("/admin")
async def admin_page():
    return FileResponse("web/admin.html")

def require_admin(init_data):
    u = verify_init_data(init_data)
    if int(u["id"]) not in ADMIN_IDS:
        raise HTTPException(403, "Admin access required")
    return int(u["id"])

@app.get("/api/admin/overview")
async def admin_overview(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    with closing(db()) as con:
        users_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        balance = con.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        earned = con.execute("SELECT COALESCE(SUM(total_earned),0) FROM users").fetchone()[0]
        withdrawn = con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='approved'").fetchone()[0]
        wr = con.execute("""SELECT w.id,w.user_id,w.amount,w.method,w.value,
                 COALESCE(NULLIF(u.username,''),NULLIF(u.first_name,''),'User')
                 FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id
                 WHERE w.status='pending' ORDER BY w.id DESC LIMIT 100""").fetchall()
        ur = con.execute("""SELECT id,COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User'),
                 referrals,balance,total_earned FROM users ORDER BY id DESC LIMIT 100""").fetchall()
    return {
        "users":users_count,"balance":balance,"earned":earned,"withdrawn":withdrawn,
        "withdrawals":[{"id":r[0],"user_id":r[1],"amount":r[2],"method":r[3],"value":r[4],"user":r[5]} for r in wr],
        "user_list":[{"id":r[0],"user":r[1],"referrals":r[2],"balance":r[3],"earned":r[4]} for r in ur]
    }

@app.post("/api/admin/withdrawal")
async def admin_withdrawal(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    wid=int(payload.get("id",0)); action=str(payload.get("action",""))
    if action not in ("approve","reject"): raise HTTPException(400,"Invalid action")
    with closing(db()) as con:
        row=con.execute("SELECT user_id,amount,status FROM withdrawals WHERE id=?",(wid,)).fetchone()
        if not row: raise HTTPException(404,"Withdrawal not found")
        if row[2] != "pending": raise HTTPException(400,"Already processed")
        now=int(time.time())
        if action=="approve":
            con.execute("UPDATE withdrawals SET status='approved' WHERE id=?",(wid,))
            note=f"Withdrawal #{wid} approved"
        else:
            con.execute("UPDATE withdrawals SET status='rejected' WHERE id=?",(wid,))
            con.execute("UPDATE users SET balance=balance+? WHERE id=?",(row[1],row[0]))
            con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",(row[0],"withdrawal_refund",row[1],f"Withdrawal #{wid} rejected",now))
            note=f"Withdrawal #{wid} rejected and refunded"
        con.commit()
    return {"ok":True,"status":action,"message":note}

@app.get("/api/admin/force-join")
async def admin_force_join_list(x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("SELECT id,chat_id,title,invite_link,active FROM force_join_channels ORDER BY id DESC").fetchall()
    return {"channels":[{"id":r[0],"chat_id":r[1],"title":r[2],"invite_link":r[3],"active":bool(r[4])} for r in rows]}

@app.post("/api/admin/force-join")
async def admin_force_join_add(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    chat_id=str(payload.get("chat_id","")).strip()
    title=str(payload.get("title","")).strip() or chat_id
    invite=str(payload.get("invite_link","")).strip()
    if not chat_id:
        raise HTTPException(400,"Channel username or chat ID is required")
    try:
        # Validate that the bot can resolve/access the channel. This also catches
        # channels where the bot is not an administrator in most cases.
        await tg_bot.get_chat(chat_id)
    except Exception as e:
        raise HTTPException(400,"Bot cannot access this channel. Add the bot as an administrator and check the @username/chat ID.")
    with closing(db()) as con:
        con.execute("INSERT INTO force_join_channels(chat_id,title,invite_link,active,created_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,invite_link=excluded.invite_link,active=1",(chat_id,title,invite,1,int(time.time())))
        con.commit()
    return {"ok":True}

@app.post("/api/admin/force-join/toggle")
async def admin_force_join_toggle(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    cid=int(payload.get("id",0)); active=1 if bool(payload.get("active")) else 0
    with closing(db()) as con:
        con.execute("UPDATE force_join_channels SET active=? WHERE id=?",(active,cid)); con.commit()
    return {"ok":True,"active":bool(active)}

@app.post("/api/admin/force-join/delete")
async def admin_force_join_delete(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    cid=int(payload.get("id",0))
    with closing(db()) as con:
        con.execute("DELETE FROM force_join_channels WHERE id=?",(cid,)); con.commit()
    return {"ok":True}

@app.post("/api/admin/gift")
async def admin_create_gift(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    code=str(payload.get("code","" )).strip().upper(); amount=float(payload.get("amount",0)); uses=int(payload.get("max_uses",1))
    if not code or amount<=0 or uses<1: raise HTTPException(400,"Invalid gift details")
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO gift_codes(code,amount,max_uses,uses) VALUES(?,?,?,0)",(code,amount,uses))
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

@app.post("/api/admin/balance")
async def admin_balance(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_admin(x_telegram_init_data)
    uid=int(payload.get("user_id",0)); amount=float(payload.get("amount",0))
    if uid<=0 or amount<=0: raise HTTPException(400,"Invalid user or amount")
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM users WHERE id=?",(uid,)).fetchone(): raise HTTPException(404,"User not found")
        con.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(amount,amount,uid))
        con.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",(uid,"admin_credit",amount,"Admin panel credit",int(time.time())))
        con.commit()
    return {"ok":True,"amount":amount}

@app.get("/api/me")
async def me(x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data)
    missing = await force_join_status(int(u["id"]))
    if missing: raise HTTPException(403, "Please join all required channels first")
    uid = int(u["id"])
    with closing(db()) as con:
        row = con.execute("""SELECT balance,total_earned,referrals,payout_method,payout_value
                             FROM users WHERE id=?""", (uid,)).fetchone()
        tx = con.execute("""SELECT type,amount,note,created_at FROM transactions
                            WHERE user_id=? ORDER BY id DESC LIMIT 20""", (uid,)).fetchall()
    if not row:
        raise HTTPException(404, "User not registered")
    return {
        "user": {"id": uid, "name": u.get("first_name",""), "username": u.get("username","")},
        "balance": row[0], "total_earned": row[1], "referrals": row[2],
        "payout_method": row[3], "payout_value": row[4],
        "referral_link": f"https://t.me/{BOT_USERNAME}?start={uid}",
        "transactions": [{"type":a,"amount":b,"note":c,"created_at":d} for a,b,c,d in tx],
        "reward": REFERRAL_REWARD, "min_withdrawal": MIN_WITHDRAWAL
    }

@app.get("/api/leaderboard")
async def leaderboard(x_telegram_init_data: str | None = Header(default=None)):
    verify_init_data(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("""
            SELECT u.id, COALESCE(NULLIF(u.username,''), NULLIF(u.first_name,''), 'User') AS display_name,
                   u.referrals, COALESCE(SUM(w.amount),0) AS total_withdrawal
            FROM users u
            LEFT JOIN withdrawals w ON w.user_id=u.id
            GROUP BY u.id
            ORDER BY u.referrals DESC, total_withdrawal DESC, u.created_at ASC
            LIMIT 20
        """).fetchall()
    return {"leaderboard":[{"user_id":r[0],"user":r[1],"referrals":r[2],"total_withdrawal":r[3]} for r in rows]}

@app.post("/api/payout")
async def payout(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data); uid = int(u["id"])
    if await force_join_status(uid): raise HTTPException(403, "Please join all required channels first")
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
    if await force_join_status(uid): raise HTTPException(403, "Please join all required channels first")
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

@app.post("/api/gift")
async def gift(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data); uid = int(u["id"])
    if await force_join_status(uid): raise HTTPException(403, "Please join all required channels first")
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
    kb.adjust(2,2,2)
    inline_rows = []
    if WEBAPP_URL:
        inline_rows.append([InlineKeyboardButton(text="📱 Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
    inline = InlineKeyboardMarkup(inline_keyboard=inline_rows) if inline_rows else None
    text = "🎉 Welcome to BotByte Refer & Earn Bot!\n\nEarn ₹3 for every successful referral.\nMinimum withdrawal: ₹20."
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
    if not await enforce_force_join(message):
        return
    await send_home(message)

@dp.callback_query(F.data == "force_join_check")
async def force_join_check(callback: CallbackQuery):
    user = callback.from_user
    missing = await force_join_status(user.id)
    await callback.answer("Checking…")
    if missing:
        rows=[]
        for _,chat_id,title,invite_link in missing:
            if invite_link:
                rows.append([InlineKeyboardButton(text=f"📢 {title}", url=invite_link)])
            elif str(chat_id).startswith("@"):
                rows.append([InlineKeyboardButton(text=f"📢 {title}", url=f"https://t.me/{str(chat_id)[1:]}")])
        rows.append([InlineKeyboardButton(text="🔄 I've Joined — Check Again", callback_data="force_join_check")])
        try:
            await callback.message.edit_text("🔒 <b>Join Required Channels</b>\n\nYou still need to join all required channels.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
        except Exception:
            pass
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_home(callback.message)

@dp.message(Command("adminpanel"))
async def adminpanel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("❌ You are not authorized to use the admin panel.")
    if not WEBAPP_URL:
        return await message.answer("❌ Admin panel is not configured yet.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛠 Open Admin Panel", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))
    ]])
    await message.answer("🛠 Admin Panel\n\nTap below to open the admin Mini App.", reply_markup=kb)

@dp.message(F.text == "🎁 Balance")
async def balance(message: Message):
    if not await enforce_force_join(message): return
    ensure_user(message.from_user)
    u = get_user(message.from_user.id)
    await message.answer(f"💰 Balance: ₹{u[3]:.2f}\n💵 Total earned: ₹{u[4]:.2f}\n👥 Referrals: {u[6]}")

@dp.message(F.text == "💕 Refer & Earn")
async def refer(message: Message):
    if not await enforce_force_join(message): return
    ensure_user(message.from_user)
    link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    await message.answer(f"💕 Refer & Earn\n\nEarn ₹{REFERRAL_REWARD:g} for each successful referral.\n\nYour link:\n{link}\n\nShare it with your friends!")

@dp.message(F.text == "🏦 Payout Method")
async def payout_method(message: Message):
    if not await enforce_force_join(message): return
    ensure_user(message.from_user)
    pending_action[message.from_user.id] = "payout"
    await message.answer(
        "🏦 Payout Method\n\n"
        "Send your payout details in this format:\n"
        "UPI yourname@upi\n\n"
        "Or for bank: Bank account-number/IFSC"
    )

@dp.message(F.text == "🏆 Leaderboard")
async def bot_leaderboard(message: Message):
    if not await enforce_force_join(message): return
    ensure_user(message.from_user)
    with closing(db()) as con:
        rows = con.execute("""
            SELECT COALESCE(NULLIF(u.username,''), NULLIF(u.first_name,''), 'User'),
                   u.referrals, COALESCE(SUM(w.amount),0)
            FROM users u LEFT JOIN withdrawals w ON w.user_id=u.id
            GROUP BY u.id
            ORDER BY u.referrals DESC, COALESCE(SUM(w.amount),0) DESC, u.created_at ASC
            LIMIT 10
        """).fetchall()
    if not rows:
        return await message.answer("🏆 Leaderboard\n\nNo users yet.")
    lines = []
    for i,(name,refs,wd) in enumerate(rows,1):
        lines.append(f"{i}. {name} — 👥 {refs} referrals — 💸 ₹{wd:.2f} withdrawn")
    await message.answer("🏆 Top Referrers\\n\\n" + "\\n".join(lines))

@dp.message(F.text == "🚀 Withdraw")
async def withdraw_help(message: Message):
    if not await enforce_force_join(message): return
    ensure_user(message.from_user)
    pending_action[message.from_user.id] = "withdraw"
    await message.answer(f"🚀 Send the amount you want to withdraw.\\nMinimum: ₹{MIN_WITHDRAWAL:g}\\nExample: 20")

@dp.message(F.text == "🎉 Gift Code")
async def gift_help(message: Message):
    if not await enforce_force_join(message): return
    ensure_user(message.from_user)
    pending_action[message.from_user.id] = "gift"
    await message.answer("🎉 Send your gift code now. Example: BONUS20")

@dp.message(F.text == "🤑 Earn More")
async def earn_more(message: Message):
    if not await enforce_force_join(message): return
    await message.answer("🤑 Earn More\n\nMore earning tasks can be added here by the admin.")

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
        con.execute("INSERT OR REPLACE INTO gift_codes(code,amount,max_uses,uses) VALUES(?,?,?,0)",(code,amount,uses))
        con.commit()
    await message.answer(f"Gift code created: {code} = ₹{amount:g}")

@dp.message(Command("withdrawals"))
async def admin_withdrawals(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    with closing(db()) as con:
        rows = con.execute("""SELECT id,user_id,amount,method,value,status FROM withdrawals
                              WHERE status='pending' ORDER BY id DESC LIMIT 20""").fetchall()
    if not rows: return await message.answer("No pending withdrawals.")
    txt = "\n".join(f"#{r[0]} | user {r[1]} | ₹{r[2]:g} | {r[3]}: {r[4]}" for r in rows)
    await message.answer("Pending withdrawals:\n\n"+txt)

@dp.message()
async def pending_actions(message: Message):
    if not await enforce_force_join(message): return
    uid = message.from_user.id
    action = pending_action.get(uid)
    if not action or not message.text:
        return
    text = message.text.strip()
    try:
        if action == "payout":
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or parts[0].lower() not in ("upi","bank"):
                return await message.answer("❌ Format: UPI yourname@upi\\nOr: Bank account-number/IFSC")
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
