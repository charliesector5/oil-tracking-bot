import os
import logging
from datetime import datetime
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, Dispatcher
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Flask setup
app = Flask(__name__)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram bot setup
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# Google credentials from Render ENV variables
google_creds = {
    "type": os.environ.get("GDRIVE_TYPE"),
    "project_id": os.environ.get("GDRIVE_PROJECT_ID"),
    "private_key_id": os.environ.get("GDRIVE_PRIVATE_KEY_ID"),
    "private_key": os.environ.get("GDRIVE_PRIVATE_KEY").replace("\\n", "\n"),
    "client_email": os.environ.get("GDRIVE_CLIENT_EMAIL"),
    "client_id": os.environ.get("GDRIVE_CLIENT_ID"),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{os.environ.get('GDRIVE_CLIENT_EMAIL')}"
}

# Google Sheets auth
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
credentials = ServiceAccountCredentials.from_json_keyfile_dict(google_creds, scope)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1  # Use the first sheet

# Telegram Bot App
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()

# /clockoff handler
async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    row = [
        datetime.now().strftime('%Y-%m-%d'),  # Date
        user.id,                              # Telegram ID
        user.full_name,                       # Name
        "Clock Off",                          # Action
        "", "", "", "",                       # Current Off, +/- Off, Final Off, Approved By
        "",                                   # Remarks
        timestamp                             # Timestamp
    ]

    try:
        sheet.append_row(row)
        await update.message.reply_text("✅ Clock off recorded successfully.")
        logger.info(f"Clock off recorded for {user.full_name} ({user.id})")
    except Exception as e:
        logger.error(f"Error writing to Google Sheet: {e}")
        await update.message.reply_text("❌ Failed to log clock off. Please try again later.")

# Add handlers
application.add_handler(CommandHandler("clockoff", clockoff))

# Webhook route
@app.route("/", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    application.update_queue.put(update)
    return "OK"

# Webhook setup route (only run once if needed)
@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    bot.set_webhook(url=WEBHOOK_URL)
    return f"Webhook set to {WEBHOOK_URL}"

if __name__ == "__main__":
    app.run(port=5000)
