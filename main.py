import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Enable logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram bot token and Google Sheet ID from environment
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Google Sheets setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

# Flask app
app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# Telegram command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OIL Bot is online.")

application.add_handler(CommandHandler("start", start))

# Flask route for webhook
@app.post("/")
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"

# Set webhook on startup
@app.before_first_request
def init_webhook():
    webhook_url = os.getenv("RENDER_EXTERNAL_URL") or "https://your-app-url.onrender.com/"
    application.bot.delete_webhook()
    application.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

if __name__ == "__main__":
    app.run(debug=False, port=10000)
