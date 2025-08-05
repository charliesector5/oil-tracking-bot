import logging
import os
import asyncio
import pytz
from datetime import datetime
from flask import Flask, request, Response
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackContext,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Logging Configuration ===
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# === Environment Variables ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://your-app.onrender.com
SHEET_NAME = os.getenv("SHEET_NAME", "Sector 5 Charlie Oil Record")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "OIL Record")
CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/secrets/credentials.json")

# === Flask App Setup ===
flask_app = Flask(__name__)
telegram_app = None  # will hold the telegram Application object

# === Google Sheets Setup ===
def setup_sheets():
    logger.info("📄 Connecting to Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    client = gspread.authorize(credentials)
    sheet = client.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    logger.info("✅ Google Sheets initialized and worksheet loaded.")
    return sheet

sheet = setup_sheets()

# === Command Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📩 /start command received from {update.effective_user.full_name}")
    await update.message.reply_text("👋 Welcome! Oil Tracking Bot is online and ready.")

# === Telegram Bot Setup ===
async def main():
    global telegram_app
    logger.info("⚙️ Building Telegram Application...")
    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    logger.info("📦 Registering command handlers...")
    telegram_app.add_handler(CommandHandler("start", start))

    logger.info(f"🌐 Setting webhook: {WEBHOOK_URL}")
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL)

# === Webhook Endpoint ===
@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def webhook() -> Response:
    update = Update.de_json(request.get_json(force=True), bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status=200)

# === Health Check ===
@flask_app.route("/", methods=["GET"])
def health():
    return "✅ Oil Tracking Bot is running."

# === Entrypoint ===
if __name__ == "__main__":
    logger.info("🚀 Starting Oil Tracking Bot initialization")

    # Patch for environments where event loop is already running (e.g., Render)
    import nest_asyncio
    nest_asyncio.apply()

    asyncio.run(main())
    flask_app.run(host="0.0.0.0", port=10000)
