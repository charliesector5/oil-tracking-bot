import os
import logging
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)
from telegram.ext._application import Application as AppClass
from telegram.ext._utils.webhookhandler import _WebhookHandler

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

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

# --- Telegram Setup ---
TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # e.g. https://yourapp.onrender.com
PORT = int(os.environ.get("PORT", 10000))

# Create Telegram Application instance
application: AppClass = Application.builder().token(TOKEN).build()

# --- Bot Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracker Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.full_name
    telegram_id = user.id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sheet.append_row([now, telegram_id, name, "Clock Off", "", "", "", "", "Via Bot", now])
    await update.message.reply_text("Clocked off successfully!")

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# --- Webhook Endpoint ---
@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        application.process_update(update)  # Synchronous update processing
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return "ERROR", 500
    return "OK", 200

# Health check
@flask_app.route("/")
def index():
    logger.info("Health check ping received at /")
    return "Bot is running", 200

# --- Webhook Setup (Run Once) ---
async def set_webhook():
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    logger.info("Webhook has been set.")

# --- Entrypoint ---
if __name__ == "__main__":
    import asyncio
    asyncio.run(set_webhook())

    # Start Flask app with Gunicorn
    flask_app.run(host="0.0.0.0", port=PORT)
