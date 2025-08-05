import os
import logging
import asyncio
import nest_asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Logging Configuration ===
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Load Environment Variables ===
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = "/etc/secrets/credentials.json"

# === Google Sheets Setup ===
def init_google_sheet():
    logger.info("📄 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    logger.info("✅ Google Sheets initialized and worksheet loaded.")
    return sheet

worksheet = init_google_sheet()

# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"👤 /start by {user.first_name} ({user.id})")
    await update.message.reply_text(f"Hello {user.first_name}! 👋 This is the Oil Tracking Bot.")

# === Main async setup ===
async def main():
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN not set. Aborting.")
        return

    logger.info("🚀 Starting Oil Tracking Bot initialization")
    logger.info("⚙️ Building Telegram Application...")

    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))

    logger.info("🌐 Setting webhook URL...")
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
    await telegram_app.start()
    logger.info("✅ Bot is now listening for updates via webhook.")

# === Flask App for Webhook ===
flask_app = Flask(__name__)

@flask_app.route("/")
def health_check():
    return "OK", 200

@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return "OK", 200

# === Run App ===
if __name__ == "__main__":
    logger.info(f"✅ Telegram token loaded: {'Yes' if TELEGRAM_TOKEN else 'No'}")
    nest_asyncio.apply()
    asyncio.run(main())
