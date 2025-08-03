import logging
import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Google Sheets Setup ---
SPREADSHEET_NAME = "Sector 5 Charlie Oil Record"
WORKSHEET_NAME = "OIL Record"
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gc = gspread.authorize(credentials)
worksheet = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# --- Flask Setup ---
app = Flask(__name__)

# --- Telegram Bot Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://your-render-url.onrender.com/<BOT_TOKEN>

application = Application.builder().token(BOT_TOKEN).build()

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the OIL Tracker Bot!")

async def clockoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        name = user.full_name
        telegram_id = user.id
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        worksheet.append_row([
            datetime.now().strftime("%d-%m-%Y"),  # Date
            str(telegram_id),                    # Telegram ID
            name,                                # Name
            "Clock Off",                         # Action
            "", "", "", "",                      # Placeholders for Off info
            "",                                  # Approved by
            "",                                  # Remarks
            timestamp                            # Timestamp
        ])
        await update.message.reply_text("✅ Clock-off recorded successfully!")
    except Exception as e:
        logger.exception("Error during /clockoff")
        await update.message.reply_text("❌ Failed to record clock-off.")

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("clockoff", clockoff))

# --- Flask Webhook Route ---
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run(application.process_update(update))
    except Exception as e:
        logger.exception("Error processing update:")
    return "OK"

@app.route("/", methods=["HEAD", "GET"])
def health_check():
    logger.info("Health check ping received at /")
    return "OK"

# --- Start Bot + Webhook ---
if __name__ == "__main__":
    logger.info("Starting bot...")
    import threading
    def run_app():
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    threading.Thread(target=run_app).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
