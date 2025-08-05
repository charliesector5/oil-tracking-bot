import os
import logging
import asyncio
import httpx
import nest_asyncio
from dotenv import load_dotenv
from flask import Flask, request

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables (if using .env locally)
load_dotenv()

# Load credentials and configs
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
CREDENTIALS_PATH = "/etc/secrets/credentials.json"

# Initialize Flask app
app = Flask(__name__)

# Google Sheets Setup
def init_google_sheets():
    logger.info("📄 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    sheet = client.open(GOOGLE_SHEET_ID).worksheet("OIL Record")
    logger.info("✅ Google Sheets initialized and worksheet loaded.")
    return sheet

worksheet = init_google_sheets()

# Telegram Bot Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracking Bot!")

# Telegram Application Setup
async def main():
    logger.info("✅ Telegram token loaded: %s", bool(TELEGRAM_TOKEN))
    logger.info("🚀 Starting Oil Tracking Bot initialization")

    logger.info("⚙️ Building Telegram Application...")
    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))

    logger.info("🌐 Setting webhook URL...")
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            params={"url": WEBHOOK_URL}
        )

    logger.info("✅ Bot is now listening for updates via webhook.")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()  # Still required for async init (but won’t use polling)

# Flask endpoint for Telegram webhook
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), None)
    await application.process_update(update)
    return "OK"

# Startup
if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
