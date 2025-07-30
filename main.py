import os
import json
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# Load environment
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google Sheet setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("phonic-command-467516-c9-3beb65d71ac7.json", scope)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(SHEET_ID).sheet1  # Assumes the first tab

# Flask app
app = Flask(__name__)
application = Application.builder().token(TOKEN).build()

# ===========================
# Telegram Handlers
# ===========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/start from {update.effective_user.id}")
    await update.message.reply_text("Welcome to OIL Tracker Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/clockoff from {user.id}")
    
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    new_row = [
        now.strftime("%Y-%m-%d"),
        user.id,
        f"{user.first_name} {user.last_name or ''}".strip(),
        "Clock Off",
        "",  # Current number of off (to be filled manually)
        "+1",  # Add/Subtract
        "",  # Final number of off (to be filled manually)
        "",  # Approved by
        "",  # Remarks
        timestamp,
    ]
    
    try:
        sheet.append_row(new_row)
        await update.message.reply_text("Clocked off successfully.")
    except Exception as e:
        logger.exception("Failed to log to Google Sheet.")
        await update.message.reply_text("Failed to log your clock-off. Try again later.")

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# Flask routes
@app.route("/")
def index():
    logger.info("Health check")
    return "OIL Bot is running", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data)
        update = Update.de_json(data, application.bot)
        logger.info(f"Incoming update: {update.to_dict()}")
        application.update_queue.put_nowait(update)
    except Exception as e:
        logger.exception("Webhook error")
    return "", 200

if __name__ == "__main__":
    logger.info("Starting Flask app")
    app.run(debug=False, host="0.0.0.0", port=10000)
