import os
import logging
import asyncio
import pytz
from datetime import datetime
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ------------------- Logging Setup -------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------- Google Sheets Setup -------------------
logger.info("🚀 Starting Oil Tracking Bot initialization")

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials_path = "/etc/secrets/credentials.json"
spreadsheet_name = "Sector 5 Charlie Oil Record"
worksheet_name = "OIL Record"

try:
    creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
    client = gspread.authorize(creds)
    sheet = client.open(spreadsheet_name).worksheet(worksheet_name)
    logger.info("✅ Google Sheets initialized and worksheet loaded.")
except Exception as e:
    logger.exception("❌ Failed to initialize Google Sheets.")
    raise

# ------------------- Telegram Bot Setup -------------------
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com/<TOKEN>
PORT = int(os.getenv("PORT", 10000))

app = Flask(__name__)

# ------------------- Telegram Command Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info(f"📩 Received /start from {user.id} ({user.first_name})")
    await update.message.reply_text("Hello! Oil tracking bot is up and running.")

# ------------------- Main Runner -------------------
async def main():
    logger.info("⚙️ Building Telegram Application...")
    telegram_app = Application.builder().token(TOKEN).build()

    logger.info("📦 Registering command handlers...")
    telegram_app.add_handler(CommandHandler("start", start))

    logger.info(f"🌐 Setting webhook: {WEBHOOK_URL}")
    await telegram_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL
    )

# ------------------- Flask Healthcheck Route -------------------
@app.route("/")
def health_check():
    logger.info("✅ Health check ping received")
    return "OK", 200

# ------------------- Entrypoint -------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("❌ Bot crashed during startup")
