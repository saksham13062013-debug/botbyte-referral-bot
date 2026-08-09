# BotByte V6 — Admin Panel via /adminpanel

- Regular users do **not** see an Admin Panel button in the bot.
- Admin uses `/adminpanel` in Telegram.
- The bot replies with **🛠 Open Admin Panel**, which opens the admin Mini App at `/admin`.
- Admin APIs verify Telegram Mini App `initData` and `ADMIN_IDS` server-side.
- Keep `ADMIN_IDS` in Render environment variables.

## Render
Set:
- `BOT_TOKEN`
- `BOT_USERNAME`
- `ADMIN_IDS`
- `REFERRAL_REWARD`
- `MIN_WITHDRAWAL`
- `WEBAPP_URL` = your Render service URL

Redeploy after replacing the files.
