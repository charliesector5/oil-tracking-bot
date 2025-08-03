import logging
import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Spreadsheet config
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
worksheet_name = "OIL Record"

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Google Sheets credentials
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
client = gspread.authorize(credentials)
sheet = client.open(SPREADSHEET_NAME).worksheet(worksheet_name)

# Flask app for Render deployment
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET", "HEAD"])
def health_check():
    logger.info("Health check ping received at /")
    return "OK", 200

# Bot command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracking Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        name = user.full_name
        telegram_id = user.id
        values = [
            datetime.now().strftime("%Y-%m-%d"),
            str(telegram_id),
            name,
            "Clocked Off",
            "", "", "", "",  # Leave Off columns blank for now
            "",  # Remarks
            now  # Timestamp
        ]
        sheet.append_row(values)
        await update.message.reply_text(f"Clock off recorded for {name} at {now}")
        logger.info(f"Clock off recorded: {values}")
    except Exception as e:
        logger.error(f"Error during /clockoff: {e}")
        await update.message.reply_text("Failed to record clock off. Please try again later.")

# Set up bot
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# Run webhook on Render
if __name__ == "__main__":
    logger.info("Starting bot...")

    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        webhook_url=WEBHOOK_URL,
        app=flask_app
    )
