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
            id INTEGER
