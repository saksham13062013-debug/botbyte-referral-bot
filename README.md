# BotByte Refer & Earn + Telegram Mini App

Configured:
- Bot username: @Botbyteamak_bot
- Referral reward: ₹3
- Minimum withdrawal: ₹20
- SQLite database
- Referral tracking
- Gift codes
- Withdrawal requests
- Payout method
- Telegram Mini App dashboard

## Setup

1. Create the bot with @BotFather and copy the bot token.
2. Copy `.env.example` to `.env`.
3. Set `BOT_TOKEN`.
4. Set `ADMIN_IDS` to your Telegram numeric user ID.
5. Deploy this project on a host with HTTPS and set `WEBAPP_URL` to the HTTPS URL.
6. Run `pip install -r requirements.txt && python bot.py`.

The bot automatically sets its private-chat menu button to the Mini App URL when `WEBAPP_URL` is configured.

## Admin commands

- `/addbalance USER_ID AMOUNT`
- `/gift CODE AMOUNT MAX_USES`
- `/withdrawals`

## Important

Keep the bot token secret. Mini App `initData` is validated server-side before balance, payout, gift-code, or withdrawal operations.
