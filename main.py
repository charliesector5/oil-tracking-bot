import os
import logging
from datetime import datetime
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask setup
app = Flask(__name__)

# Telegram & Google Sheet Setup
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

# Google credentials from ENV
google_creds = {
    "type": os.environ["GDRIVE_TYPE"],
    "project_id": os.environ["GDRIVE_PROJECT_ID"],
    "private_key_id": os.environ["GDRIVE_PRIVATE_KEY_ID"],
    "private_key": os.environ["GDRIVE_PRIVATE_KEY"].replace("\\n", "\n"),
    "client_email": os.environ["GDRIVE_CLIENT_EMAIL"],
    "client_id": os.environ["GDRIVE_CLIENT_ID"],
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{os.environ['GDRIVE_CLIENT_EMAIL']}"
}

# Google Sheets setup
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
credentials = ServiceAccountCredentials.from_json_keyfile_dict(google_creds, scope)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# Telegram Bot setup
bot = Bot(BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()

# Command: /clockoff
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    row = [
        now.strftime("%Y-%m-%d"),    # Date
        user.id,                     # Telegram ID
        user.full_name,             # Name
        "Clock Off",                # Action
        "", "", "", "",             # Placeholder columns
        "",                         # Remarks
        timestamp                   # Timestamp
    ]

    try:
        sheet.append_row(row)
        await update.message.reply_text("✅ Clock off recorded successfully.")
        logger.info(f"Clocked off: {user.full_name} ({user.id})")
    except Exception as e:
        await update.message.reply_text("❌ Failed to log clock off. Please try again.")
        logger.error(f"Error writing to Google Sheet: {e}")

# Register handler
application.add_handler(CommandHandler("clockoff", clockoff))

# Flask endpoint for webhook
@app.route("/", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    asyncio.run(application.process_update(update))
    return "ok"

# Set webhook once
@app.route("/set_webhook")
def set_webhook():
    success = bot.set_webhook(WEBHOOK_URL)
    return f"Webhook set: {success}"

if __name__ == "__main__":
    app.run(port=5000)
