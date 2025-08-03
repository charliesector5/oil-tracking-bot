import os
import logging
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
)

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Google Sheets Setup ---
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"

creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)
sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# --- Environment Variables ---
TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # e.g., https://oil-tracking-bot.onrender.com
PORT = int(os.environ.get("PORT", 10000))

# --- Bot Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracker Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.full_name
    telegram_id = user.id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sheet.append_row([now, telegram_id, name, "Clock Off", "", "", "", "", "Via Bot", now])
    await update.message.reply_text("Clocked off successfully!")

# --- Main App Setup ---
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clockoff", clockoff))

    # Set webhook
    await app.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")

    # Start aiohttp webhook server
    async def handle(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response()

    aio_app = web.Application()
    aio_app.router.add_post(f'/{TOKEN}', handle)
    aio_app.router.add_get("/", lambda request: web.Response(text="OK"))

    logger.info("Starting aiohttp server...")
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("Bot is live at port %s", PORT)

    await app.initialize()
    await app.start()
    await app.updater.start_polling()  # optional fallback
    await app.updater.wait()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
