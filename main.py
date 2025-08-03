import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Telegram Bot Token ===
BOT_TOKEN = os.getenv("BOT_TOKEN")

# === Google Sheets Setup ===
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
SPREADSHEET_NAME = "OIL Tracker"
worksheet_name = "Sheet1"

# Authorize Google Sheets
creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)
sheet = client.open(SPREADSHEET_NAME).worksheet(worksheet_name)

# === Flask app for health checks ===
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    logger.info("Health check ping received at /")
    return "OK", 200

# === Telegram Handlers ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start triggered by {user.full_name} ({user.id})")
    await update.message.reply_text("Welcome to the OIL bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        datetime.now().strftime("%Y-%m-%d"),
        str(user.id),
        user.full_name,
        "Clock Off",
        "", "", "", "",  # placeholders for balance tracking
        "Clocked off via bot",
        now
    ]
    sheet.append_row(row)
    logger.info(f"Clock off recorded for {user.full_name} ({user.id}) at {now}")
    await update.message.reply_text("Your clock off has been recorded!")

# === Main async application ===

async def main():
    # Hardcoded webhook URL (change if needed)
    webhook_url = f"https://your-service-name.onrender.com/{BOT_TOKEN}"

    # Init Telegram app
    app = Application.builder().token(BOT_TOKEN).build()

    # Add command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clockoff", clockoff))

    logger.info("Bot started.")

    # Start the webhook server
    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )

# Entry point
if __name__ == "__main__":
    try:
        import threading
        threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=8080)).start()
        asyncio.run(main())
    except Exception as e:
        logger.exception("An error occurred while running the bot:")
