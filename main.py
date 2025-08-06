import os
import logging
import json
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Logging config
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Load credentials from the mounted secret file
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
with open(SERVICE_ACCOUNT_FILE, "r") as f:
    creds_json = json.load(f)

# Google Sheets API setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)

try:
    gc = gspread.authorize(credentials)
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    sh = gc.open_by_key(sheet_id)
    worksheet = sh.get_worksheet(0)
    logging.info("✅ Google Sheets initialized and worksheet loaded.")
except Exception as e:
    logging.error(f"❌ Google Sheets initialization failed: {e}")

# Telegram Bot setup
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context):
    await update.message.reply_text("Hello! This bot is up and running.")

application.add_handler(CommandHandler("start", start))

# Set webhook on startup
@app.before_first_request
def set_webhook():
    import httpx
    response = httpx.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        params={"url": WEBHOOK_URL},
    )
    if response.status_code == 200:
        logging.info("🚀 Webhook has been set.")
    else:
        logging.error(f"❌ Failed to set webhook: {response.text}")

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.create_task(application.process_update(update))
    return "OK"

@app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot is running!"

if __name__ == "__main__":
    logging.info("🟢 Starting Flask server to keep the app alive...")
    app.run(host="0.0.0.0", port=10000)
