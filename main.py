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
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Config ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # e.g., https://your-app-name.onrender.com
PORT = int(os.environ.get("PORT", 10000))

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Flask App ---
flask_app = Flask(__name__)

# --- Google Sheets Setup ---
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"

creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
client = gspread.authorize(creds)
sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# --- Telegram App ---
application = Application.builder().token(BOT_TOKEN).build()

# --- Telegram Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracker Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.full_name
    telegram_id = user.id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sheet.append_row([now, telegram_id, name, "Clock Off", "", "", "", "", "Via Bot", now])
    await update.message.reply_text("Clocked off successfully!")

# --- Register Handlers ---
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# --- Flask Routes ---
@flask_app.route("/")
def index():
    logger.info("Health check ping received at /")
    return "OK", 200

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(application.process_update(update))
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        return "Error", 500

# --- Async Startup ---
async def setup():
    logger.info("Starting bot...")
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("Webhook set.")

# --- Entry Point ---
if __name__ == "__main__":
    asyncio.run(setup())
    flask_app.run(host="0.0.0.0", port=PORT)
