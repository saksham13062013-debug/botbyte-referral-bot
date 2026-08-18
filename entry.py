import json
import hmac
import hashlib
import time
from urllib.parse import urlparse, parse_qsl
from workers import WorkerEntrypoint, Response
from js import fetch, Object
from pyodide.ffi import to_js


# Cloudflare Worker backend for BotByte.
# Required Worker secrets/vars:
# BOT_TOKEN       = Telegram bot token
# BOT_USERNAME    = bot username without @ (optional; falls back to "Botbyteamak_bot")
# ADMIN_IDS       = comma-separated Telegram user IDs
# REFERRAL_REWARD = default 3
# MIN_WITHDRAWAL = default 20
# WEBHOOK_SECRET  = optional Telegram webhook secret
#
# The existing Web/index.html and Web/admin.html use the /api/* routes below.
# D1 binding must be named DB and assets binding must be named ASSETS.


def _env(self, name, default=""):
    value = getattr(self.env, name, None)
    if value is None:
        return default
    return str(value)


def _num(self, name, default):
    try:
        return float(_env(self, name, str(default)))
    except Exception:
        return float(default)


def _admins(self):
    raw = _env(self, "ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _bot_token(self):
    return _env(self, "BOT_TOKEN", "")


def _bot_username(self):
    return _env(self, "BOT_USERNAME", "Botbyteamak_bot").lstrip("@") or "Botbyteamak_bot"


def _reward(self):
    return _num(self, "REFERRAL_REWARD", 3)


def _min_withdrawal(self):
    return _num(self, "MIN_WITHDRAWAL", 20)


def json_response(data, status=200):
    return Response.json(data, status=status)


async def db_all(self, sql, *args):
    stmt = self.env.DB.prepare(sql)
    if args:
        stmt = stmt.bind(*args)
    result = await stmt.all()
    return result.results


async def db_run(self, sql, *args):
    stmt = self.env.DB.prepare(sql)
    if args:
        stmt = stmt.bind(*args)
    return await stmt.run()


async def ensure_schema(self):
    # All application data lives in D1. This also makes a fresh database usable
    # without requiring a separate migration before the first request.
    statements = [
        """CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            total_earned REAL NOT NULL DEFAULT 0,
            referred_by INTEGER,
            referrals INTEGER NOT NULL DEFAULT 0,
            payout_method TEXT,
            payout_value TEXT,
            created_at INTEGER NOT NULL,
            leaderboard_visible INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            method TEXT,
            value TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS gift_codes(
            code TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS gift_redemptions(
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(code,user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS force_join_channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            username TEXT,
            invite_link TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS earn_more(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            reward REAL NOT NULL DEFAULT 0,
            url TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bot_messages(
            key TEXT PRIMARY KEY,
            text TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bot_emojis(
            key TEXT PRIMARY KEY,
            custom_emoji_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS bot_pending(
            user_id INTEGER PRIMARY KEY,
            action TEXT NOT NULL
        )""",
    ]
    for sql in statements:
        await db_run(self, sql)

    # Lightweight migration for an older users table.
    cols = await db_all(self, "PRAGMA table_info(users)")
    names = {str(r.get("name", "")) for r in cols}
    if "leaderboard_visible" not in names:
        try:
            await db_run(self, "ALTER TABLE users ADD COLUMN leaderboard_visible INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass

    defaults = {
        "welcome": "🎉 Welcome to BotByte Refer & Earn Bot!\n\nEarn ₹{reward} for every successful referral.\nMinimum withdrawal: ₹{min_withdrawal}.",
        "balance": "💰 Balance: ₹{balance:.2f}\n💵 Total earned: ₹{total_earned:.2f}\n👥 Referrals: {referrals}",
        "refer": "💕 Refer & Earn\n\nEarn ₹{reward} for each successful referral.\n\nYour link:\n{referral_link}\n\nShare it with your friends!",
        "gift": "🎉 Send your gift code now. Example: BONUS20",
        "withdraw": "🚀 Send the amount you want to withdraw.\nMinimum: ₹{min_withdrawal}\nExample: 20",
        "payout": "🏦 Send your payout details in this format:\nUPI yourname@upi\n\nOr for bank: BANK account-number/IFSC",
        "earn_more": "🤑 Earn More\n\nMore earning tasks can be added here by the admin.",
        "leaderboard": "🏆 Top Referrers",
    }
    for key, text in defaults.items():
        await db_run(self, "INSERT OR IGNORE INTO bot_messages(key,text) VALUES(?,?)", key, text)


def render_message(text, user, reward, minimum, referral_link):
    vals = {
        "name": user.get("first_name", ""),
        "username": user.get("username", ""),
        "balance": float(user.get("balance", 0)),
        "total_earned": float(user.get("total_earned", 0)),
        "referrals": int(user.get("referrals", 0)),
        "reward": reward,
        "min_withdrawal": minimum,
        "referral_link": referral_link,
    }
    try:
        return text.format(**vals)
    except Exception:
        return text


async def get_message(self, key, fallback):
    rows = await db_all(self, "SELECT text FROM bot_messages WHERE key=?", key)
    return rows[0]["text"] if rows else fallback


async def telegram(self, method, payload):
    token = _bot_token(self)
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    opts = to_js(
        {
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False),
        },
        dict_converter=Object.fromEntries,
    )
    response = await fetch(url, opts)
    data = await response.json()
    return data


async def send_message(self, chat_id, text, reply_markup=None):
    payload = {"chat_id": int(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await telegram(self, "sendMessage", payload)


async def set_bot_menu(self, worker_url):
    webapp_url = worker_url.rstrip("/")
    try:
        await telegram(
            self,
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "📱 Dashboard",
                    "web_app": {"url": webapp_url},
                }
            },
        )
    except Exception as e:
        print("setChatMenuButton failed:", e)


async def setup_telegram(self, worker_url):
    token = _bot_token(self)
    if not token:
        return {"ok": False, "error": "BOT_TOKEN secret is missing"}
    webhook = worker_url.rstrip("/") + "/api/telegram/webhook"
    payload = {"url": webhook, "drop_pending_updates": False}
    secret = _env(self, "WEBHOOK_SECRET", "")
    if secret:
        payload["secret_token"] = secret
    result = await telegram(self, "setWebhook", payload)
    await set_bot_menu(self, worker_url)
    try:
        await telegram(
            self,
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Open BotByte"},
                    {"command": "adminpanel", "description": "Open admin panel"},
                ]
            },
        )
    except Exception:
        pass
    return {"webhook": webhook, "telegram": result}


def verify_init_data(self, init_data):
    if not init_data:
        raise ValueError("Telegram initData required")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received = data.pop("hash", None)
    if not received:
        raise ValueError("Invalid initData")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", _bot_token(self).encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("Invalid Telegram signature")
    try:
        auth_date = int(data.get("auth_date", "0"))
    except Exception:
        raise ValueError("Invalid auth_date")
    if abs(time.time() - auth_date) > 86400:
        raise ValueError("Expired Telegram data")
    user = json.loads(data.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("Telegram user missing")
    return user


def init_user(user, referrer_id=None):
    return user, referrer_id


async def ensure_user(self, user, referrer_id=None):
    uid = int(user["id"])
    now = int(time.time())
    rows = await db_all(self, "SELECT id FROM users WHERE id=?", uid)
    if rows:
        await db_run(
            self,
            "UPDATE users SET username=?, first_name=? WHERE id=?",
            user.get("username", ""),
            user.get("first_name", ""),
            uid,
        )
        return False

    valid_ref = None
    if referrer_id and int(referrer_id) != uid:
        r = await db_all(self, "SELECT id FROM users WHERE id=?", int(referrer_id))
        if r:
            valid_ref = int(referrer_id)

    await db_run(
        self,
        """INSERT INTO users(id,username,first_name,referred_by,created_at,leaderboard_visible)
           VALUES(?,?,?,?,?,1)""",
        uid,
        user.get("username", ""),
        user.get("first_name", ""),
        valid_ref,
        now,
    )
    if valid_ref:
        reward = _reward(self)
        await db_run(
            self,
            "UPDATE users SET balance=balance+?, total_earned=total_earned+?, referrals=referrals+1 WHERE id=?",
            reward,
            reward,
            valid_ref,
        )
        await db_run(
            self,
            "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
            valid_ref,
            "referral",
            reward,
            f"Referral: {uid}",
            now,
        )
        try:
            ref_rows = await db_all(
                self,
                "SELECT balance,total_earned,referrals,first_name,username FROM users WHERE id=?",
                valid_ref,
            )
            if ref_rows:
                try:
                    ru = dict(ref_rows[0])
                    msg = await get_message(self, "balance", "💰 Balance: ₹{balance:.2f}")
                    await send_message(
                        self,
                        valid_ref,
                        render_message(
                            "🎉 Referral Success!\n\n" + msg,
                            {
                                "first_name": ru.get("first_name", ""),
                                "username": ru.get("username", ""),
                                "balance": ru.get("balance", 0),
                                "total_earned": ru.get("total_earned", 0),
                                "referrals": ru.get("referrals", 0),
                            },
                            reward,
                            _min_withdrawal(self),
                            f"https://t.me/{_bot_username(self)}?start={valid_ref}",
                        ),
                    )
                except Exception:
                    pass
        except Exception:
            pass
    return True


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🎁 Balance"}, {"text": "💕 Refer & Earn"}],
            [{"text": "🎉 Gift Code"}, {"text": "🚀 Withdraw"}],
            [{"text": "🏦 Payout Method"}, {"text": "🤑 Earn More"}],
            [{"text": "🏆 Leaderboard"}],
        ],
        "resize_keyboard": True,
    }


async def send_home(self, chat_id, user):
    reward = _reward(self)
    minimum = _min_withdrawal(self)
    link = f"https://t.me/{_bot_username(self)}?start={int(user['id'])}"
    rows = await db_all(
        self,
        "SELECT balance,total_earned,referrals FROM users WHERE id=?",
        int(user["id"]),
    )
    u = dict(rows[0]) if rows else {"balance": 0, "total_earned": 0, "referrals": 0}
    template = await get_message(self, "welcome", "🎉 Welcome to BotByte!\n\nEarn ₹{reward} for every successful referral.\nMinimum withdrawal: ₹{min_withdrawal}.")
    text = render_message(
        template,
        {"first_name": user.get("first_name", ""), "username": user.get("username", ""), **u},
        reward,
        minimum,
        link,
    )
    await send_message(self, chat_id, text, main_keyboard())
    await send_message(
        self,
        chat_id,
        "Open your dashboard:",
        {"inline_keyboard": [[{"text": "📱 Open Mini App", "web_app": {"url": self._worker_url + "/"}}]]},
    )


async def handle_text(self, self, message):
    user = message.get("from") or {}
    chat_id = message.get("chat", {}).get("id")
    if not user.get("id") or chat_id is None:
        return
    text = (message.get("text") or "").strip()
    uid = int(user["id"])

    ref = None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].isdigit():
            ref = int(parts[1])
    await ensure_user(self, user, ref)

    if text.startswith("/start"):
        await send_home(self, chat_id, user)
        return

    if text == "/adminpanel":
        if uid not in _admins(self):
            await send_message(self, chat_id, "❌ You are not authorized to use the admin panel.")
            return
        await send_message(
            self,
            chat_id,
            "🛠 Admin Panel",
            {"inline_keyboard": [[{"text": "🛠 Open Admin Panel", "web_app": {"url": self._worker_url.rstrip("/") + "/admin"}}]]},
        )
        return

    rows = await db_all(
        self,
        "SELECT balance,total_earned,referrals,payout_method,payout_value,first_name,username FROM users WHERE id=?",
        uid,
    )
    u = dict(rows[0]) if rows else {}
    link = f"https://t.me/{_bot_username(self)}?start={uid}"
    reward = _reward(self)
    minimum = _min_withdrawal(self)

    if text == "🎁 Balance":
        template = await get_message(self, "balance", "💰 Balance: ₹{balance:.2f}\n💵 Total earned: ₹{total_earned:.2f}\n👥 Referrals: {referrals}")
        await send_message(self, chat_id, render_message(template, u, reward, minimum, link))
        return

    if text == "💕 Refer & Earn":
        template = await get_message(self, "refer", "💕 Refer & Earn\n\nEarn ₹{reward} for each successful referral.\n\nYour link:\n{referral_link}")
        await send_message(self, chat_id, render_message(template, u, reward, minimum, link))
        return

    if text == "🎉 Gift Code":
        template = await get_message(self, "gift", "🎉 Send your gift code now.")
        await send_message(self, chat_id, render_message(template, u, reward, minimum, link))
        await db_run(self, "INSERT OR REPLACE INTO bot_pending(user_id,action) VALUES(?,?)", uid, "gift")
        return

    if text == "🚀 Withdraw":
        template = await get_message(self, "withdraw", "🚀 Send the amount you want to withdraw.\nMinimum: ₹{min_withdrawal}\nExample: 20")
        await send_message(self, chat_id, render_message(template, u, reward, minimum, link))
        await db_run(self, "INSERT OR REPLACE INTO bot_pending(user_id,action) VALUES(?,?)", uid, "withdraw")
        return

    if text == "🏦 Payout Method":
        template = await get_message(self, "payout", "🏦 Send payout details:\nUPI yourname@upi\nor BANK account-number/IFSC")
        await send_message(self, chat_id, render_message(template, u, reward, minimum, link))
        await db_run(self, "INSERT OR REPLACE INTO bot_pending(user_id,action) VALUES(?,?)", uid, "payout")
        return

    if text == "🤑 Earn More":
        items = await db_all(self, "SELECT title,message,reward,url FROM earn_more WHERE active=1 ORDER BY id DESC")
        if not items:
            template = await get_message(self, "earn_more", "🤑 Earn More\n\nMore earning tasks can be added here by the admin.")
            await send_message(self, chat_id, render_message(template, u, reward, minimum, link))
        else:
            chunks = ["🤑 Earn More\n"]
            for x in items[:10]:
                line = f"• {x['title']}"
                if x.get("reward"):
                    line += f" — ₹{float(x['reward']):g}"
                line += f"\n{x['message']}"
                if x.get("url"):
                    line += f"\n{x['url']}"
                chunks.append(line)
            await send_message(self, chat_id, "\n\n".join(chunks))
        return

    if text == "🏆 Leaderboard":
        rows = await db_all(
            self,
            """SELECT COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User') AS user,
                      referrals
               FROM users
               WHERE leaderboard_visible=1
               ORDER BY referrals DESC, created_at ASC LIMIT 10""",
        )
        if not rows:
            await send_message(self, chat_id, "🏆 Leaderboard\n\nNo users yet.")
        else:
            lines = [f"{i}. {r['user']} — 👥 {r['referrals']}" for i, r in enumerate(rows, 1)]
            await send_message(self, chat_id, "🏆 Top Referrers\n\n" + "\n".join(lines))
        return

    pending = await db_all(self, "SELECT action FROM bot_pending WHERE user_id=?", uid)
    if pending and text:
        action = pending[0]["action"]
        try:
            if action == "payout":
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or parts[0].lower() not in ("upi", "bank"):
                    await send_message(self, chat_id, "❌ Format: UPI yourname@upi\nOr: BANK account-number/IFSC")
                    return
                await db_run(self, "UPDATE users SET payout_method=?, payout_value=? WHERE id=?", parts[0].upper(), parts[1].strip(), uid)
                await send_message(self, chat_id, "✅ Payout method saved.")
            elif action == "withdraw":
                amount = float(text)
                if amount < minimum:
                    await send_message(self, chat_id, f"❌ Minimum withdrawal is ₹{minimum:g}")
                    return
                row = await db_all(self, "SELECT balance,payout_method,payout_value FROM users WHERE id=?", uid)
                if not row or float(row[0]["balance"]) < amount:
                    await send_message(self, chat_id, "❌ Insufficient balance.")
                    return
                if not row[0]["payout_method"] or not row[0]["payout_value"]:
                    await send_message(self, chat_id, "❌ Set your payout method first.")
                    return
                now = int(time.time())
                await db_run(self, "UPDATE users SET balance=balance-? WHERE id=?", amount, uid)
                await db_run(
                    self,
                    "INSERT INTO withdrawals(user_id,amount,method,value,status,created_at) VALUES(?,?,?,?,?,?)",
                    uid, amount, row[0]["payout_method"], row[0]["payout_value"], "pending", now,
                )
                await db_run(
                    self,
                    "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                    uid, "withdrawal", -amount, "Withdrawal request", now,
                )
                await send_message(self, chat_id, f"✅ Withdrawal request submitted: ₹{amount:g}")
            elif action == "gift":
                code = text.upper()
                g = await db_all(self, "SELECT amount,max_uses,uses FROM gift_codes WHERE code=?", code)
                if not g or int(g[0]["uses"]) >= int(g[0]["max_uses"]):
                    await send_message(self, chat_id, "❌ Invalid or exhausted gift code.")
                    return
                used = await db_all(self, "SELECT 1 AS x FROM gift_redemptions WHERE code=? AND user_id=?", code, uid)
                if used:
                    await send_message(self, chat_id, "❌ Code already redeemed.")
                    return
                amount = float(g[0]["amount"])
                now = int(time.time())
                await db_run(self, "INSERT INTO gift_redemptions(code,user_id) VALUES(?,?)", code, uid)
                await db_run(self, "UPDATE gift_codes SET uses=uses+1 WHERE code=?", code)
                await db_run(self, "UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?", amount, amount, uid)
                await db_run(self, "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)", uid, "gift", amount, code, now)
                await send_message(self, chat_id, f"🎉 Gift code redeemed! ₹{amount:g} added to your balance.")
            await db_run(self, "DELETE FROM bot_pending WHERE user_id=?", uid)
        except Exception:
            await send_message(self, chat_id, "❌ Something went wrong. Please try again.")


def require_user(self, request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    try:
        return verify_init_data(self, init_data)
    except Exception as e:
        raise ValueError(str(e))


async def require_admin(self, request):
    user = require_user(self, request)
    if int(user["id"]) not in _admins(self):
        raise PermissionError("Admin access required")
    return user


async def body_json(request):
    try:
        return await request.json()
    except Exception:
        return {}


async def me(self, request):
    user = require_user(self, request)
    uid = int(user["id"])
    rows = await db_all(self, "SELECT balance,total_earned,referrals,payout_method,payout_value FROM users WHERE id=?", uid)
    if not rows:
        await ensure_user(self, user)
        rows = await db_all(self, "SELECT balance,total_earned,referrals,payout_method,payout_value FROM users WHERE id=?", uid)
    r = rows[0]
    tx = await db_all(self, "SELECT type,amount,note,created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20", uid)
    return {
        "user": {"id": uid, "name": user.get("first_name", ""), "username": user.get("username", "")},
        "balance": float(r["balance"]),
        "total_earned": float(r["total_earned"]),
        "referrals": int(r["referrals"]),
        "payout_method": r["payout_method"],
        "payout_value": r["payout_value"],
        "referral_link": f"https://t.me/{_bot_username(self)}?start={uid}",
        "transactions": [dict(x) for x in tx],
        "reward": _reward(self),
        "min_withdrawal": _min_withdrawal(self),
    }


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        self._worker_url = request.url.split("/api/", 1)[0].rstrip("/")
        url = urlparse(request.url)
        path = url.path

        try:
            await ensure_schema(self)

            if path == "/api/health":
                setup = await setup_telegram(self, self._worker_url)
                return json_response({"ok": True, "service": "botbyte-worker", "telegram": setup})

            if path == "/api/telegram/setup":
                setup = await setup_telegram(self, self._worker_url)
                return json_response(setup)

            if path == "/api/telegram/webhook":
                secret = _env(self, "WEBHOOK_SECRET", "")
                if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
                    return json_response({"ok": False, "error": "forbidden"}, 403)
                update = await request.json()
                message = update.get("message")
                if message:
                    await handle_text(self, message)
                return json_response({"ok": True})

            if path == "/api/db-test":
                rows = await db_all(self, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                return json_response({"ok": True, "tables": rows})

            if path == "/api/me":
                return json_response(await me(self, request))

            if path == "/api/me/balance":
                d = await me(self, request)
                return json_response({"ok": True, "balance": d["balance"]})

            if path == "/api/leaderboard":
                require_user(self, request)
                rows = await db_all(
                    self,
                    """SELECT COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User') AS user,
                              id AS user_id, referrals
                       FROM users
                       WHERE leaderboard_visible=1
                       ORDER BY referrals DESC, created_at ASC LIMIT 20""",
                )
                return json_response({"leaderboard": [dict(x) for x in rows]})

            if path == "/api/payout" and request.method == "POST":
                user = require_user(self, request)
                data = await body_json(request)
                method = str(data.get("method", "")).strip().upper()
                value = str(data.get("value", "")).strip()
                if method not in ("UPI", "BANK") or not value:
                    return json_response({"ok": False, "detail": "Payout method and value are required"}, 400)
                await db_run(self, "UPDATE users SET payout_method=?,payout_value=? WHERE id=?", method, value, int(user["id"]))
                return json_response({"ok": True})

            if path == "/api/withdraw" and request.method == "POST":
                user = require_user(self, request)
                uid = int(user["id"])
                data = await body_json(request)
                try:
                    amount = float(data.get("amount"))
                except Exception:
                    return json_response({"ok": False, "detail": "Invalid amount"}, 400)
                minimum = _min_withdrawal(self)
                if amount < minimum:
                    return json_response({"ok": False, "detail": f"Minimum withdrawal is ₹{minimum:g}"}, 400)
                row = await db_all(self, "SELECT balance,payout_method,payout_value FROM users WHERE id=?", uid)
                if not row or float(row[0]["balance"]) < amount:
                    return json_response({"ok": False, "detail": "Insufficient balance"}, 400)
                if not row[0]["payout_method"] or not row[0]["payout_value"]:
                    return json_response({"ok": False, "detail": "Set payout method first"}, 400)
                now = int(time.time())
                await db_run(self, "UPDATE users SET balance=balance-? WHERE id=?", amount, uid)
                await db_run(self, "INSERT INTO withdrawals(user_id,amount,method,value,status,created_at) VALUES(?,?,?,?,?,?)",
                             uid, amount, row[0]["payout_method"], row[0]["payout_value"], "pending", now)
                await db_run(self, "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                             uid, "withdrawal", -amount, "Withdrawal request", now)
                try:
                    await send_message(self, uid, f"⏳ Withdrawal Processing\n\n₹{amount:g} request received.")
                except Exception:
                    pass
                return json_response({"ok": True})

            if path in ("/api/gift", "/api/gift/claim") and request.method == "POST":
                user = require_user(self, request)
                uid = int(user["id"])
                data = await body_json(request)
                code = str(data.get("code", "")).strip().upper()
                if not code:
                    return json_response({"ok": False, "detail": "Enter a gift code"}, 400)
                g = await db_all(self, "SELECT amount,max_uses,uses FROM gift_codes WHERE code=?", code)
                if not g or int(g[0]["uses"]) >= int(g[0]["max_uses"]):
                    return json_response({"ok": False, "detail": "Invalid or exhausted gift code"}, 400)
                if await db_all(self, "SELECT 1 AS x FROM gift_redemptions WHERE code=? AND user_id=?", code, uid):
                    return json_response({"ok": False, "detail": "Code already redeemed"}, 400)
                amount = float(g[0]["amount"])
                now = int(time.time())
                await db_run(self, "INSERT INTO gift_redemptions(code,user_id) VALUES(?,?)", code, uid)
                await db_run(self, "UPDATE gift_codes SET uses=uses+1 WHERE code=?", code)
                await db_run(self, "UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?", amount, amount, uid)
                await db_run(self, "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)", uid, "gift", amount, code, now)
                try:
                    await send_message(self, uid, f"🎉 Gift Code\n\n₹{amount:g} added to your balance.")
                except Exception:
                    pass
                return json_response({"ok": True, "amount": amount})

            if path == "/api/force-join/channels":
                require_user(self, request)
                rows = await db_all(self, "SELECT id,chat_id,title,username,invite_link,active FROM force_join_channels ORDER BY id")
                return json_response({"channels": [{"id": r["id"], "chat_id": r["chat_id"], "title": r["title"], "username": r["username"], "invite_link": r["invite_link"], "enabled": bool(r["active"])} for r in rows]})

            if path == "/api/force-join/verify" and request.method == "POST":
                user = require_user(self, request)
                channels = await db_all(self, "SELECT chat_id,title,username,active FROM force_join_channels WHERE active=1")
                if not channels:
                    return json_response({"ok": True, "joined": True})
                for c in channels:
                    try:
                        r = await telegram(self, "getChatMember", {"chat_id": c["chat_id"], "user_id": int(user["id"])})
                        status = (((r or {}).get("result") or {}).get("status") or "")
                        if status in ("left", "kicked", ""):
                            return json_response({"ok": True, "joined": False})
                    except Exception:
                        return json_response({"ok": True, "joined": False})
                return json_response({"ok": True, "joined": True})

            # ---------------- Admin API ----------------
            if path == "/api/admin/overview":
                await require_admin(self, request)
                users_count = (await db_all(self, "SELECT COUNT(*) AS n FROM users"))[0]["n"]
                balance = (await db_all(self, "SELECT COALESCE(SUM(balance),0) AS n FROM users"))[0]["n"]
                earned = (await db_all(self, "SELECT COALESCE(SUM(total_earned),0) AS n FROM users"))[0]["n"]
                withdrawn = (await db_all(self, "SELECT COALESCE(SUM(amount),0) AS n FROM withdrawals WHERE status='paid'"))[0]["n"]
                wr = await db_all(
                    self,
                    """SELECT w.id,w.user_id,w.amount,w.method,w.value,w.status,
                              COALESCE(NULLIF(u.username,''),NULLIF(u.first_name,''),'User') AS user
                       FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id
                       WHERE w.status IN ('pending','processing')
                       ORDER BY w.id DESC LIMIT 100""",
                )
                ur = await db_all(
                    self,
                    """SELECT id,COALESCE(NULLIF(username,''),NULLIF(first_name,''),'User') AS user,
                              referrals,balance,total_earned,leaderboard_visible
                       FROM users ORDER BY id DESC LIMIT 100""",
                )
                return json_response({
                    "users": int(users_count),
                    "balance": float(balance),
                    "earned": float(earned),
                    "withdrawn": float(withdrawn),
                    "withdrawals": [dict(x) for x in wr],
                    "user_list": [dict(x) for x in ur],
                })

            if path == "/api/admin/withdrawal" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                wid = int(data.get("id", 0))
                action = str(data.get("action", ""))
                row = await db_all(self, "SELECT user_id,amount,status FROM withdrawals WHERE id=?", wid)
                if not row:
                    return json_response({"detail": "Withdrawal not found"}, 404)
                current = row[0]["status"]
                uid = int(row[0]["user_id"])
                amount = float(row[0]["amount"])
                if action == "processing":
                    if current != "pending":
                        return json_response({"detail": "Already processed"}, 400)
                    await db_run(self, "UPDATE withdrawals SET status='processing' WHERE id=?", wid)
                    try: await send_message(self, uid, f"⏳ Withdrawal Processing\n\n₹{amount:g} is being processed.")
                    except Exception: pass
                    return json_response({"ok": True, "status": "processing"})
                if action == "paid":
                    if current != "processing":
                        return json_response({"detail": "Withdrawal must be processing first"}, 400)
                    await db_run(self, "UPDATE withdrawals SET status='paid' WHERE id=?", wid)
                    try: await send_message(self, uid, f"✅ Withdrawal Paid\n\n₹{amount:g} has been marked as paid.")
                    except Exception: pass
                    return json_response({"ok": True, "status": "paid"})
                if action == "reject":
                    if current not in ("pending", "processing"):
                        return json_response({"detail": "Already processed"}, 400)
                    await db_run(self, "UPDATE withdrawals SET status='rejected' WHERE id=?", wid)
                    await db_run(self, "UPDATE users SET balance=balance+? WHERE id=?", amount, uid)
                    await db_run(self, "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                                 uid, "withdrawal_refund", amount, f"Withdrawal #{wid} rejected", int(time.time()))
                    try: await send_message(self, uid, f"❌ Withdrawal Rejected\n\n₹{amount:g} returned to your balance.")
                    except Exception: pass
                    return json_response({"ok": True, "status": "rejected"})
                return json_response({"detail": "Invalid action"}, 400)

            if path == "/api/admin/gift" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                code = str(data.get("code", "")).strip().upper()
                amount = float(data.get("amount", 0))
                uses = int(data.get("max_uses", 1))
                if not code or amount <= 0 or uses < 1:
                    return json_response({"detail": "Invalid gift details"}, 400)
                await db_run(self, "INSERT OR REPLACE INTO gift_codes(code,amount,max_uses,uses) VALUES(?,?,?,0)", code, amount, uses)
                return json_response({"ok": True, "code": code, "amount": amount, "max_uses": uses})

            if path == "/api/admin/balance" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                uid = int(data.get("user_id", 0))
                amount = float(data.get("amount", 0))
                if uid <= 0 or amount == 0:
                    return json_response({"detail": "Invalid user or amount"}, 400)
                if not await db_all(self, "SELECT 1 AS x FROM users WHERE id=?", uid):
                    return json_response({"detail": "User not found"}, 404)
                await db_run(self, "UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?", amount, max(amount, 0), uid)
                await db_run(self, "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES(?,?,?,?,?)",
                             uid, "admin_credit" if amount > 0 else "admin_debit", amount, "Admin panel adjustment", int(time.time()))
                try:
                    await send_message(self, uid, ("💰 Balance Added" if amount > 0 else "💸 Balance Adjusted") + f"\n\nAmount: ₹{abs(amount):g}")
                except Exception:
                    pass
                r = await db_all(self, "SELECT balance FROM users WHERE id=?", uid)
                return json_response({"ok": True, "amount": amount, "balance": float(r[0]["balance"])})

            if path == "/api/admin/broadcast" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                text = str(data.get("text", "")).strip()
                if not text or len(text) > 4096:
                    return json_response({"detail": "Invalid broadcast text"}, 400)
                ids = await db_all(self, "SELECT id FROM users ORDER BY id")
                sent = failed = 0
                for r in ids:
                    try:
                        result = await send_message(self, int(r["id"]), text)
                        if result.get("ok"):
                            sent += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
                return json_response({"ok": True, "total": len(ids), "sent": sent, "failed": failed})

            if path == "/api/admin/leaderboard" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                uid = int(data.get("user_id", 0))
                visible = 1 if bool(data.get("visible")) else 0
                await db_run(self, "UPDATE users SET leaderboard_visible=? WHERE id=?", visible, uid)
                return json_response({"ok": True})

            if path == "/api/admin/messages" and request.method == "GET":
                await require_admin(self, request)
                rows = await db_all(self, "SELECT key,text FROM bot_messages ORDER BY key")
                return json_response({"messages": [dict(x) for x in rows]})

            if path == "/api/admin/messages" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                key = str(data.get("key", "")).strip()
                text = str(data.get("text", ""))
                if not key:
                    return json_response({"detail": "Missing message key"}, 400)
                await db_run(self, "INSERT OR REPLACE INTO bot_messages(key,text) VALUES(?,?)", key, text)
                return json_response({"ok": True})

            if path == "/api/admin/earn-more" and request.method == "GET":
                await require_admin(self, request)
                rows = await db_all(self, "SELECT id,title,message,reward,url,active FROM earn_more ORDER BY id DESC")
                return json_response({"items": [dict(x) for x in rows]})

            if path == "/api/admin/earn-more" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                title = str(data.get("title", "")).strip()
                message = str(data.get("message", "")).strip()
                reward = float(data.get("reward", 0) or 0)
                url_value = str(data.get("url", "")).strip()
                if not title or not message:
                    return json_response({"detail": "Title and message are required"}, 400)
                await db_run(self, "INSERT INTO earn_more(title,message,reward,url,active,created_at) VALUES(?,?,?,?,1,?)",
                             title, message, reward, url_value, int(time.time()))
                return json_response({"ok": True})

            if path == "/api/admin/earn-more/toggle" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                await db_run(self, "UPDATE earn_more SET active=? WHERE id=?", 1 if bool(data.get("active")) else 0, int(data.get("id", 0)))
                return json_response({"ok": True})

            if path == "/api/admin/earn-more/delete" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                await db_run(self, "DELETE FROM earn_more WHERE id=?", int(data.get("id", 0)))
                return json_response({"ok": True})

            if path == "/api/admin/force-join" and request.method == "GET":
                await require_admin(self, request)
                rows = await db_all(self, "SELECT id,chat_id,title,username,invite_link,active FROM force_join_channels ORDER BY id")
                return json_response({"channels": [dict(x) for x in rows]})

            if path == "/api/admin/force-join" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                chat_id = str(data.get("chat_id", "")).strip()
                title = str(data.get("title", "")).strip() or chat_id
                invite = str(data.get("invite_link", "")).strip()
                username = chat_id.lstrip("@") if chat_id.startswith("@") else ""
                if not chat_id:
                    return json_response({"detail": "Channel is required"}, 400)
                await db_run(self, "INSERT OR REPLACE INTO force_join_channels(chat_id,title,username,invite_link,active) VALUES(?,?,?,?,1)",
                             chat_id, title, username, invite)
                return json_response({"ok": True})

            if path == "/api/admin/force-join/toggle" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                await db_run(self, "UPDATE force_join_channels SET active=? WHERE id=?", 1 if bool(data.get("active")) else 0, int(data.get("id", 0)))
                return json_response({"ok": True})

            if path == "/api/admin/force-join/delete" and request.method == "POST":
                await require_admin(self, request)
                data = await body_json(request)
                await db_run(self, "DELETE FROM force_join_channels WHERE id=?", int(data.get("id", 0)))
                return json_response({"ok": True})

            if path == "/api/admin/emojis":
                await require_admin(self, request)
                rows = await db_all(self, "SELECT key,custom_emoji_id FROM bot_emojis ORDER BY key")
                return json_response({"emojis": [dict(x) for x in rows]})

            if path == "/api/notifications/event" and request.method == "POST":
                require_user(self, request)
                # Notification calls are intentionally lightweight; core bot events
                # already send their own Telegram messages.
                return json_response({"ok": True})

            # /admin is a virtual route; the actual file is Web/admin.html.
            if path == "/admin":
                return await self.env.ASSETS.fetch(self._worker_url + "/admin.html")

            # Static Mini App and admin assets.
            return await self.env.ASSETS.fetch(request)

        except PermissionError as e:
            return json_response({"ok": False, "detail": str(e)}, 403)
        except ValueError as e:
            return json_response({"ok": False, "detail": str(e)}, 401)
        except Exception as e:
            print("Worker error:", repr(e))
            return json_response({"ok": False, "detail": str(e)}, 500)
