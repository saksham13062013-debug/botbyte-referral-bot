import os, json, hmac, hashlib, time, sqlite3, asyncio
from urllib.parse import parse_qsl
from contextlib import closing

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
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

@app.get("/")
async def home():
    return FileResponse("web/index.html")

@app.get("/api/me")
async def me(x_telegram_init_data: str | None = Header(default=None)):
    u = verify_init_data(x_telegram_init_data)
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
    kb.adjust(2,2,2)
    inline = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]) if WEBAPP_URL else None
    text = "🎉 Welcome to BotByte Refer & Earn Bot!\\n\\nEarn ₹3 for every successful referral.\\nMinimum withdrawal: ₹20."
    await message.answer(text, reply_markup=kb.as_markup(resize_keyboard=True))
    if inline:
        await message.answer("Open your dashboard:", reply_markup=inline)

dp = Dispatcher()

@dp.message(CommandStart())
async def start(message: Message):
    ref = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit():
        ref = int(parts[1])
    ensure_user(message.from_user, ref)
    await send_home(message)

@dp.message(F.text == "🎁 Balance")
async def balance(message: Message):
    ensure_user(message.from_user)
    u = get_user(message.from_user.id)
    await message.answer(f"💰 Balance: ₹{u[3]:.2f}\\n💵 Total earned: ₹{u[4]:.2f}\\n👥 Referrals: {u[6]}")

@dp.message(F.text == "💕 Refer & Earn")
async def refer(message: Message):
    ensure_user(message.from_user)
    link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    await message.answer(f"💕 Refer & Earn\\n\\nEarn ₹{REFERRAL_REWARD:g} for each successful referral.\\n\\nYour link:\\n{link}\\n\\nShare it with your friends!")

@dp.message(F.text == "🏦 Payout Method")
async def payout_method(message: Message):
    await message.answer("Use the Mini App to securely set your payout method and UPI/other payout details.")

@dp.message(F.text == "🚀 Withdraw")
async def withdraw_help(message: Message):
    await message.answer(f"🚀 Minimum withdrawal: ₹{MIN_WITHDRAWAL:g}\\nOpen the Mini App to submit your withdrawal request.")

@dp.message(F.text == "🎉 Gift Code")
async def gift_help(message: Message):
    await message.answer("🎉 Open the Mini App and enter your gift code.")

@dp.message(F.text == "🤑 Earn More")
async def earn_more(message: Message):
    await message.answer("🤑 Earn More\\n\\nMore earning tasks can be added here by the admin.")

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
    txt = "\\n".join(f"#{r[0]} | user {r[1]} | ₹{r[2]:g} | {r[3]}: {r[4]}" for r in rows)
    await message.answer("Pending withdrawals:\\n\\n"+txt)

async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button={"type":"web_app","text":"📱 Dashboard","web_app":{"url":WEBAPP_URL}}
        )
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT","8080")), log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(dp.start_polling(bot), server.serve())

if __name__ == "__main__":
    asyncio.run(main())
