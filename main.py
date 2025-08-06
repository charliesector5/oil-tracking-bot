import os
import logging
import asyncio
import json
import httpx
import nest_asyncio
import gspread
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

# --- Load environment and credentials ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

# --- Flask App ---
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Oil Tracking Bot is up."

@app.route('/health')
def health():
    return "✅ Health check passed."

# --- Global Variables ---
telegram_app = None
worksheet = None

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        asyncio.create_task(telegram_app.process_update(update))
        return "OK"
    return "Method Not Allowed", 405

# --- Telegram Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome to the Oil Tracking Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏰ Clock off recorded. (Stub logic)")

# --- Main Async App Initialization ---
async def main():
    global telegram_app, worksheet

    logger.info("📄 Connecting to Google Sheets...")
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sheet.sheet1
        logger.info("✅ Google Sheets initialized and worksheet loaded.")
    except Exception as e:
        logger.error(f"❌ Google Sheets initialization failed: {e}")
        return

    logger.info("⚙️ Initializing Telegram Application...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("clockoff", clockoff))

    logger.info("🌐 Setting Telegram webhook...")
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("🚀 Webhook has been set.")

# --- Entry Point ---
if __name__ == "__main__":
    nest_asyncio.apply()

    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"❌ Error during bot initialization: {e}")

    logger.info("🟢 Starting Flask server to keep the app alive...")
    app.run(host="0.0.0.0", port=10000)
