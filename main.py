import os
import logging
import asyncio
import httpx
import json
import gspread
import nest_asyncio

from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Flask App for Webhook ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "✅ Oil Tracking Bot is up."

@flask_app.route('/health')
def health():
    return "✅ Health check passed."

@flask_app.route(f'/{os.getenv("BOT_TOKEN")}', methods=["POST"])
def webhook():
    from telegram import Update
    from telegram.ext import Application

    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        telegram_app.update_queue.put_nowait(update)
        return "OK"

# --- Load environment variables (Render auto-populates these) ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_CREDENTIALS_PATH = "/etc/secrets/credentials.json"  # Fixed path for secret file
WORKSHEET_NAME = "OIL Record"  # Static unless changed manually

# --- Global app reference (used in webhook route) ---
telegram_app = None

# --- Telegram Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome to the Oil Tracking Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏰ Clock off recorded. (Stub logic)")

# --- Main async bot logic ---
async def main():
    global telegram_app

    logger.info("📄 Connecting to Google Sheets...")
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_ID)
        worksheet = sheet.worksheet(WORKSHEET_NAME)
        logger.info("✅ Google Sheets initialized and worksheet loaded.")
    except Exception as e:
        logger.error(f"❌ Google Sheets initialization failed: {e}")
        return

    logger.info(f"✅ Telegram token loaded: {'Yes' if BOT_TOKEN else 'No'}")
    logger.info("🚀 Starting Oil Tracking Bot initialization")
    logger.info("⚙️ Building Telegram Application...")

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))

    logger.info("🌐 Setting webhook URL...")
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL)

    logger.info("✅ Bot is now listening for updates via webhook.")

# --- Entry point ---
if __name__ == "__main__":
    nest_asyncio.apply()

    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"❌ Unhandled error: {e}")

    logger.info("🟢 Starting Flask server to keep the app alive...")
    flask_app.run(host="0.0.0.0", port=10000)
