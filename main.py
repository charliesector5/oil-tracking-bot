from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
import datetime
import os
import logging

# --- Flask Setup ---
app = Flask(__name__)

# --- Telegram Bot Token ---
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")

# --- Google Sheets Setup via Secret File ---
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)

# --- Open the correct Google Sheet ---
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
sheet = gc.open_by_key(SHEET_ID).sheet1

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracker Bot! Use /clockoff to log your time.")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = [
        datetime.date.today().isoformat(),      # Date
        user.id,                                # Telegram ID
        user.full_name,                         # Name
        "Clock Off",                            # Action
        "", "", "",                             # Current/Change/Final Off Balances
        "",                                     # Approved By
        "",                                     # Remarks
        now                                     # Timestamp
    ]

    try:
        sheet.append_row(row)
        await update.message.reply_text("Clock off recorded successfully.")
        logger.info(f"Clockoff recorded: {row}")
    except Exception as e:
        await update.message.reply_text("Failed to record clock off.")
        logger.error(f"Error writing to sheet: {e}")

# --- Telegram Bot Setup ---
application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# --- Webhook Handler ---
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        application.update_queue.put(update)
        return "OK"
    return "NOT OK"

# --- Health Check ---
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!"

# --- Main Entrypoint ---
if __name__ == "__main__":
    application.run_polling()
